"""Populated Phase5 SQL spine: reviewed release -> outcome -> frozen evaluation -> expiry."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.feedback.adapters.payload_repository import RetentionTransactions
from app.modules.feedback.application.retention_service import RetentionService
from app.modules.feedback.domain.evaluation import DatasetCommand
from app.modules.feedback.domain.evaluation_runs import EvaluationRequest
from app.modules.feedback.domain.outcomes import OutcomeSourceCommand
from app.modules.reporting.domain.commands import CreateReportCommand, EditReportCommand
from tests.test_evaluation_api_integration import evaluation_service
from tests.test_evaluation_curation_integration import admin_service, command
from tests.test_feedback_outcome_integration import services
from tests.test_report_api_integration import ReportHarness, pytestmark, report_harness
from tests.test_report_review_integration import publish_command, reviews, worker

__all__ = ["pytestmark", "report_harness"]

# Static SQL identifiers: each must have actual rows, never vacuous empty-table RLS checks.
PHASE5_TABLES = (
    "reports",
    "report_metric_snapshots",
    "report_versions",
    "report_snapshot_sources",
    "report_snapshot_receipts",
    "report_review_decisions",
    "report_publications",
    "report_generation_jobs",
    "report_generation_usage",
    "report_version_verifications",
    "feedback",
    "feedback_outcomes",
    "evaluation_candidates",
    "evaluation_cases",
    "evaluation_case_revisions",
    "evaluation_dataset_versions",
    "evaluation_dataset_cases",
    "evaluation_runs",
    "evaluation_results",
    "ai_retention_payloads",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["vi", "en"])
async def test_phase5_spine_preserves_history_and_isolates_every_populated_resource(
    report_harness: ReportHarness, locale: Literal["vi", "en"]
):
    h = report_harness
    initial = await h.service.create(
        actor=h.actor,
        command=CreateReportCommand(project_id=h.project_id, locale=locale),
        idempotency_key=str(uuid4()),
    )
    assert await worker(h)
    r = await h.service.get(actor=h.actor, report_id=initial.report.id)
    assert r.selected_version.narrative is not None
    # Even an identity edit is a new immutable version requiring its own verifier and human gate.
    edited = await reviews(h).edit(
        actor=h.actor,
        report_id=r.report.id,
        command=EditReportCommand(
            parent_version_id=r.selected_version.id,
            snapshot_hash=r.snapshot.snapshot_hash,
            narrative=r.selected_version.narrative,
        ),
        expected_version=r.report.version,
        idempotency_key=str(uuid4()),
    )
    assert edited.publications == () and edited.verification_state == "PENDING"
    assert await worker(h)
    checked = await h.service.get(actor=h.actor, report_id=r.report.id)
    published = await reviews(h).publish(
        actor=h.actor,
        report_id=r.report.id,
        command=publish_command(checked),
        expected_version=checked.report.version,
        idempotency_key=str(uuid4()),
    )
    feedback_id = published.terminal_outcome_id
    assert feedback_id is not None
    outcomes, _ = services(h)
    outcome = await outcomes.record(
        actor=h.actor,
        feedback_id=feedback_id,
        source=OutcomeSourceCommand(
            source_type="TASK_ACTUALS", source_id=h.task_id, source_version=0
        ),
        idempotency_key=str(uuid4()),
    )
    assert outcome.state == "UNKNOWN"  # Absent observations never become fabricated actuals.
    curation = await admin_service(h)
    candidate = await curation.prepare(
        actor=h.actor, feedback_id=feedback_id, idempotency_key=str(uuid4())
    )
    for version, case_locale in enumerate(("vi", "en"), 1):
        await curation.curate(
            actor=h.actor,
            command=command(candidate, locale=case_locale),
            expected_version=version,
            idempotency_key=str(uuid4()),
        )
    dataset = await curation.freeze_dataset(
        actor=h.actor,
        command=DatasetCommand(name=f"closure-{uuid4()}", split="GOLDEN"),
        idempotency_key=str(uuid4()),
    )
    evals = evaluation_service(h)
    run = await evals.start(
        actor=h.actor,
        request=EvaluationRequest(dataset_version_id=dataset.id),
        idempotency_key=str(uuid4()),
    )
    job = await evals.claim(worker_id="phase5-closure", organization_id=h.actor.organization_id)
    assert job is not None
    await evals.execute(job)
    result = await evals.get(actor=h.actor, run_id=run.id)
    assert result.result is not None and result.result.passed == 2
    assert result.result.hosted_quality == "NOT_RUN"
    assert not result.result.gate_passed  # Two synthetic cases cannot certify complete coverage.
    await h.sql("UPDATE memberships SET role='ADMIN' WHERE id=:id", {"id": h.foreign.membership_id})
    async with h.engine.connect() as connection, connection.begin():
        await connection.execute(text("SET LOCAL ROLE app_runtime"))
        await connection.execute(
            text("SELECT set_config('app.membership_id',:member,true)"),
            {"member": str(h.actor.membership_id)},
        )
        assert not await connection.scalar(
            text("SELECT rolbypassrls FROM pg_roles WHERE rolname=current_user")
        )
        for table in PHASE5_TABLES:
            await connection.execute(
                text("SELECT set_config('app.membership_id',:member,true)"),
                {"member": str(h.actor.membership_id)},
            )
            await connection.execute(
                text("SELECT set_config('app.organization_id',:org,true)"),
                {"org": str(h.actor.organization_id)},
            )
            owner_count = await connection.scalar(text(f"SELECT count(*) FROM {table}"))
            assert owner_count and owner_count > 0, table
            await connection.execute(
                text("SELECT set_config('app.membership_id',:member,true)"),
                {"member": str(h.foreign.membership_id)},
            )
            await connection.execute(
                text("SELECT set_config('app.organization_id',:org,true)"),
                {"org": str(h.foreign.organization_id)},
            )
            assert (
                await connection.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE organization_id=:owner"),
                    {"owner": h.actor.organization_id},
                )
                == 0
            ), table
    async with AsyncClient(
        transport=ASGITransport(app=h.app(h.foreign)), base_url="http://test"
    ) as client:
        assert (await client.get(f"/api/v1/reports/{r.report.id}")).status_code == 404
        assert (await client.get(f"/api/v1/reports/{r.report.id}/sources")).status_code == 404
        assert (
            await client.post(
                f"/api/v1/reports/{r.report.id}/publish",
                json=publish_command(checked).model_dump(mode="json"),
                headers={
                    "Idempotency-Key": str(uuid4()),
                    "If-Match": f'"{checked.report.version}"',
                },
            )
        ).status_code == 404
    before = await h.service.get(actor=h.actor, report_id=r.report.id)
    purge = await RetentionService(
        RetentionTransactions(async_sessionmaker(h.engine, expire_on_commit=False))
    ).purge_once(
        organization_id=h.actor.organization_id, now=datetime.now(UTC) + timedelta(days=31)
    )
    assert purge.purged > 0
    after = await h.service.get(actor=h.actor, report_id=r.report.id)
    assert after.snapshot == before.snapshot
    assert after.published_versions == before.published_versions
    assert after.publications == before.publications
    assert after.feedback == before.feedback
    assert after.feedback_outcomes == before.feedback_outcomes
    assert (await evals.get(actor=h.actor, run_id=run.id)).result == result.result
