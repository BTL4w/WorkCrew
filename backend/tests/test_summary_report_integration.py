# pyright: reportUnusedImport=false
# ruff: noqa: F811
"""Committed-summary conversion never recaptures live facts or duplicates a digest."""

import asyncio
import os
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.automations.domain.digests import AuthorizedJobScope
from app.modules.automations.domain.schedules import ChatScheduleCommand
from app.modules.identity.domain.auth import AuthenticatedActor
from tests.test_daily_summary_scheduler_integration import setup
from tests.test_daily_update_concurrency_integration import Case, case  # noqa:F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


@pytest.mark.asyncio
async def test_mode_default_and_patch_omission(case: Case):
    engine, actor, schedules, saved, window, _ = await setup(case)
    try:
        assert saved.narrative_mode == "NONE"
        _, draft, version = await schedules.prepare_from_chat(
            actor,
            ChatScheduleCommand(
                operation="CONFIGURE",
                project_reference=str(saved.project_id),
                narrative_mode="DRAFT_FOR_MANAGER",
            ),
            str(uuid4()),
        )
        assert draft is not None
        enabled = await schedules.confirm(actor, draft.id, version, str(uuid4()))
        assert enabled.narrative_mode == "DRAFT_FOR_MANAGER"
        _, draft, _ = await schedules.prepare_from_chat(
            actor,
            ChatScheduleCommand(
                operation="CONFIGURE", project_reference=str(saved.project_id), cutoff="01:00"
            ),
            str(uuid4()),
        )
        assert draft is not None and draft.command.narrative_mode == "DRAFT_FOR_MANAGER"
        next_window = (await schedules.get(actor, saved.project_id)).window
        assert next_window is not None and next_window.applied_version == window.applied_version
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_summary_narrative_does_not_duplicate_digest(case: Case):
    from app.modules.reporting.adapters.summary_repository import SummaryReportTransactions
    from app.modules.reporting.application.summary_report_service import SummaryReportService
    from app.modules.reporting.domain.snapshots import canonical_hash

    engine, actor, _, _, window, digests = await setup(case, narrative_mode="DRAFT_FOR_MANAGER")
    try:
        original = await digests.trigger(
            AuthorizedJobScope(actor, datetime.now(UTC)), window.id, "CUTOFF"
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service = SummaryReportService(SummaryReportTransactions(sessions, "UTC"))
        # Live Task changes after capture cannot alter converted facts.
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET status='DONE',version=version+1 WHERE id=:id"),
                {"id": case.tasks[0]},
            )
        results = await asyncio.gather(
            *[
                service.ensure_draft(
                    actor=actor,
                    summary_id=original.id,
                    locale="en",
                    workflow_version="reporting-narrative.v1",
                )
                for _ in range(2)
            ]
        )
        first = results[0]
        assert first.report.id == results[1].report.id
        assert first.report.origin == "DAILY_SUMMARY"
        assert first.report.summary_id == original.id
        assert first.report.summary_hash == canonical_hash(original.model_dump(mode="json"))
        assert first.snapshot.captured_at == original.snapshot_at
        assert first.snapshot.period.start_utc == original.window.starts_at
        assert first.snapshot.metrics["included_tasks.status.done_count"].value == 0
        assert first.snapshot.metrics["included_tasks.status.total_count"].state == "PARTIAL"
        assert "tasks.status.total_count" not in first.snapshot.metrics
        assert not first.publications
        async with engine.connect() as conn:
            for table in (
                "daily_summary_triggers",
                "daily_summary_snapshots",
                "reports",
                "report_generation_jobs",
            ):
                assert (
                    await conn.execute(
                        text(f"SELECT count(*) FROM {table} WHERE organization_id=:org"),
                        {"org": actor.organization_id},
                    )
                ).scalar_one() == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["mock", "disabled"])
async def test_committed_summary_worker_and_delivery(
    case: Case, provider: Literal["mock", "disabled"]
):
    import json
    from typing import cast

    from pydantic import BaseModel

    from app.core.config import Settings
    from app.modules.assistant.adapters.agent_runtime import CurrentActorResolverPort
    from app.modules.planning_runs.adapters.ai_runtime import build_model_gateway
    from app.modules.reporting.adapters.generation_repository import GenerationTransactions
    from app.modules.reporting.adapters.narrative_runtime import ReportNarrativeRuntime
    from app.modules.reporting.adapters.summary_repository import SummaryReportTransactions
    from app.modules.reporting.adapters.transaction import ReportTransactions
    from app.modules.reporting.application.job_service import ReportJobService
    from app.modules.reporting.application.report_service import ReportService
    from app.modules.reporting.application.summary_report_service import SummaryReportService
    from app.modules.reporting.domain.commands import PublishReportCommand
    from work_management_ai.model_gateway.contracts import (
        StructuredModelRequest,
        StructuredModelResponse,
    )

    engine, actor, _, _, window, digests = await setup(case, narrative_mode="DRAFT_FOR_MANAGER")

    class Actors:
        async def resolve(
            self, *, organization_id: UUID, membership_id: UUID
        ) -> AuthenticatedActor | None:
            return (
                actor
                if organization_id == actor.organization_id and membership_id == actor.membership_id
                else None
            )

    try:
        scope = AuthorizedJobScope(actor, datetime.now(UTC))
        summary = await digests.trigger(scope, window.id, "CUTOFF")
        assert await digests.deliver(scope, worker_id="digest")
        assert len(await digests.list(actor)) == 1
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        from app.modules.planning_runs.domain.models import OutboxEvent
        from app.modules.reporting.adapters.summary_repository import SummaryReportingPublisher

        async with engine.connect() as conn:
            event_row = (
                await conn.execute(
                    text(
                        "SELECT id,event_id,payload FROM outbox_events WHERE aggregate_id=:id "
                        "AND event_type='automation.summary.captured.v1'"
                    ),
                    {"id": summary.id},
                )
            ).one()
        event = OutboxEvent(
            id=event_row.id,
            event_id=event_row.event_id,
            organization_id=actor.organization_id,
            event_type="automation.summary.captured.v1",
            aggregate_type="daily_summary",
            aggregate_id=summary.id,
            payload=event_row.payload,
        )
        publisher = SummaryReportingPublisher(
            SummaryReportService(SummaryReportTransactions(sessions, "UTC")),
            cast(CurrentActorResolverPort, Actors()),
        )
        await publisher.publish(event)
        await publisher.publish(event)
        draft = await SummaryReportService(SummaryReportTransactions(sessions, "UTC")).ensure_draft(
            actor=actor,
            summary_id=summary.id,
            locale="vi",
            workflow_version="reporting-narrative.v1",
        )
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE projects SET name='Renamed after capture' WHERE id=:id"),
                {"id": summary.project_id},
            )
        gateway = build_model_gateway(Settings(environment="test", ai_provider=provider))

        class CapturedGateway:
            async def generate_structured[T: BaseModel](
                self, request: StructuredModelRequest[T]
            ) -> StructuredModelResponse[T]:
                if request.invocation_key.endswith(".draft"):
                    payload = json.loads(request.messages[-1].content)
                    assert payload["UNTRUSTED_CONTEXT"]["project_label"] == summary.project_name
                return await gateway.generate_structured(request)

        worker = ReportJobService(
            GenerationTransactions(sessions, "UTC"),
            ReportNarrativeRuntime(
                sessions=sessions,
                actors=cast(CurrentActorResolverPort, Actors()),
                gateway=CapturedGateway(),
                timezone="UTC",
            ),
        )
        assert await worker.run_once(worker_id="report", organization_id=actor.organization_id)
        reports = ReportService(ReportTransactions(sessions, "UTC"))
        after = await reports.get(actor=actor, report_id=draft.report.id)
        assert after.snapshot == draft.snapshot
        async with engine.connect() as conn:
            diagnostic = (
                await conn.execute(
                    text(
                        "SELECT status,stop_reason,safe_error_code,checkpoint "
                        "FROM orchestration_runs WHERE report_id=:id"
                    ),
                    {"id": draft.report.id},
                )
            ).all()
            agent_diagnostic = (
                await conn.execute(
                    text(
                        "SELECT status,safe_error_code FROM agent_runs WHERE organization_id=:org"
                    ),
                    {"org": actor.organization_id},
                )
            ).all()
        assert after.generation_state == (
            "AWAITING_REVIEW" if provider == "mock" else "AI_UNAVAILABLE"
        ), (diagnostic, agent_diagnostic)
        assert after.publications == ()
        async with engine.connect() as conn:
            assert (
                await conn.execute(
                    text("SELECT trigger_kind FROM orchestration_runs WHERE report_id=:id"),
                    {"id": draft.report.id},
                )
            ).scalar_one() == "SUMMARY_JOB"
        if provider == "mock":
            from app.modules.reporting.adapters.review_repository import ReviewTransactions
            from app.modules.reporting.application.review_service import ReviewService
            from app.modules.reporting.domain.reports import ReportError

            # Moving a captured task out of the authorized project makes its narrative unavailable.
            async with engine.begin() as conn:
                other_project = (
                    await conn.execute(
                        text("SELECT project_id FROM tasks WHERE id=:id"), {"id": case.tasks[1]}
                    )
                ).scalar_one()
                await conn.execute(
                    text("UPDATE tasks SET project_id=:project WHERE id=:id"),
                    {"project": other_project, "id": case.tasks[0]},
                )
            unavailable = await reports.get(actor=actor, report_id=draft.report.id)
            assert (
                unavailable.narrative_access_state == "UNAVAILABLE"
                and unavailable.selected_version.narrative is None
            )
            with pytest.raises(ReportError):
                await ReviewService(ReviewTransactions(sessions, "UTC")).publish(
                    actor=actor,
                    report_id=draft.report.id,
                    command=PublishReportCommand(
                        mode="REVIEWED_NARRATIVE",
                        report_version_id=after.selected_version.id,
                        snapshot_hash=after.snapshot.snapshot_hash,
                    ),
                    expected_version=after.report.version,
                    idempotency_key=str(uuid4()),
                )
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE tasks SET project_id=:project WHERE id=:id"),
                    {"project": summary.project_id, "id": case.tasks[0]},
                )
            published = await ReviewService(ReviewTransactions(sessions, "UTC")).publish(
                actor=actor,
                report_id=draft.report.id,
                command=PublishReportCommand(
                    mode="REVIEWED_NARRATIVE",
                    report_version_id=after.selected_version.id,
                    snapshot_hash=after.snapshot.snapshot_hash,
                ),
                expected_version=after.report.version,
                idempotency_key=str(uuid4()),
            )
            assert len(published.report_result.publications) == 1
        assert not await digests.deliver(scope, worker_id="digest")
        feed = await digests.list(actor)
        assert len(feed) == 1 and feed[0].report_link is not None
        assert feed[0].snapshot == summary
        assert (feed[0].report_link.publication_id is not None) == (provider == "mock")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["creator", "pause", "recipient"])
async def test_revoked_creator_cancels_generation(case: Case, revocation: str):
    from app.modules.reporting.adapters.summary_repository import SummaryReportTransactions
    from app.modules.reporting.application.summary_report_service import SummaryReportService
    from app.modules.reporting.domain.reports import ReportError

    engine, actor, schedules, saved, window, digests = await setup(
        case, narrative_mode="DRAFT_FOR_MANAGER"
    )
    try:
        summary = await digests.trigger(
            AuthorizedJobScope(actor, datetime.now(UTC)), window.id, "CUTOFF"
        )
        if revocation == "pause":
            await schedules.pause(actor, saved.id, saved.version, True, str(uuid4()))
        else:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE memberships SET role='EMPLOYEE' WHERE id=:id")
                    if revocation == "creator"
                    else text("UPDATE memberships SET is_active=false WHERE id=:id"),
                    {"id": actor.membership_id},
                )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        with pytest.raises(ReportError):
            await SummaryReportService(SummaryReportTransactions(sessions, "UTC")).ensure_draft(
                actor=actor,
                summary_id=summary.id,
                locale="vi",
                workflow_version="reporting-narrative.v1",
            )
        async with engine.connect() as conn:
            assert (
                await conn.execute(
                    text("SELECT count(*) FROM reports WHERE organization_id=:org"),
                    {"org": actor.organization_id},
                )
            ).scalar_one() == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_summary_hash_cannot_be_null(case: Case):
    from sqlalchemy.exc import IntegrityError

    from app.modules.reporting.adapters.summary_repository import SummaryReportTransactions
    from app.modules.reporting.application.summary_report_service import SummaryReportService

    engine, actor, _, _, window, digests = await setup(case, narrative_mode="DRAFT_FOR_MANAGER")
    try:
        summary = await digests.trigger(
            AuthorizedJobScope(actor, datetime.now(UTC)), window.id, "CUTOFF"
        )
        report = await SummaryReportService(
            SummaryReportTransactions(async_sessionmaker(engine, expire_on_commit=False), "UTC")
        ).ensure_draft(
            actor=actor,
            summary_id=summary.id,
            locale="vi",
            workflow_version="reporting-narrative.v1",
        )
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE reports SET summary_hash=NULL WHERE id=:id"),
                    {"id": report.report.id},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_truncated_summary_uses_included_metrics(case: Case):
    from app.modules.reporting.adapters.summary_repository import SummaryReportTransactions
    from app.modules.reporting.adapters.transaction import ReportTransactions
    from app.modules.reporting.application.report_service import ReportService
    from app.modules.reporting.application.summary_report_service import SummaryReportService
    from app.modules.reporting.domain.commands import CreateReportCommand

    engine, actor, _, saved, window, digests = await setup(case, narrative_mode="DRAFT_FOR_MANAGER")
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tasks(id,organization_id,project_id,title,status,"
                    "required_skill_labels,"
                    "created_by_membership_id,updated_by_membership_id) "
                    "VALUES(:id,:org,:project,'Included sample','TO_DO','[]',:member,:member)"
                ),
                [
                    dict(
                        id=uuid4(),
                        org=actor.organization_id,
                        project=saved.project_id,
                        member=actor.membership_id,
                    )
                    for _ in range(100)
                ],
            )
        summary = await digests.trigger(
            AuthorizedJobScope(actor, datetime.now(UTC)), window.id, "CUTOFF"
        )
        assert len(summary.tasks) == 100 and "TASK_LIMIT" in summary.unknown_inputs
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        report = await SummaryReportService(
            SummaryReportTransactions(sessions, "UTC")
        ).ensure_draft(
            actor=actor,
            summary_id=summary.id,
            locale="vi",
            workflow_version="reporting-narrative.v1",
        )
        assert report.snapshot.metrics["included_tasks.status.total_count"].value == 100
        assert report.snapshot.metrics["included_tasks.status.total_count"].state == "PARTIAL"
        assert "TASK_LIMIT" in report.snapshot.limitations
        full = await ReportService(ReportTransactions(sessions, "UTC")).create(
            actor=actor,
            command=CreateReportCommand(project_id=saved.project_id, narrative_enabled=False),
            idempotency_key=str(uuid4()),
        )
        assert full.snapshot.metrics["tasks.status.total_count"].value == 101
        assert full.snapshot.metrics["tasks.status.total_count"].state == "KNOWN"
        assert full.report.origin == "ON_DEMAND"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_employee_projection_never_contains_raw_draft(case: Case):
    from dataclasses import replace

    from app.modules.organization.domain.roles import MembershipRole
    from app.modules.reporting.adapters.summary_repository import SummaryReportTransactions
    from app.modules.reporting.application.summary_report_service import SummaryReportService
    from app.modules.reporting.domain.reports import ReportError

    engine, actor, _, saved, window, digests = await setup(
        case, narrative_mode="DRAFT_FOR_MANAGER", extra_recipient=True
    )
    try:
        scope = AuthorizedJobScope(actor, datetime.now(UTC))
        summary = await digests.trigger(scope, window.id, "CUTOFF")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service = SummaryReportService(SummaryReportTransactions(sessions, "UTC"))
        report = await service.ensure_draft(
            actor=actor,
            summary_id=summary.id,
            locale="vi",
            workflow_version="reporting-narrative.v1",
        )
        while await digests.deliver(scope, worker_id="projection"):
            pass
        manager_feed = await digests.list(actor)
        assert (
            manager_feed[0].report_link is not None
            and manager_feed[0].report_link.report_id == report.report.id
        )
        member = next(m for m in saved.recipients if m != actor.membership_id)
        async with engine.connect() as conn:
            user = (
                await conn.execute(
                    text("SELECT user_id FROM memberships WHERE id=:id"), {"id": member}
                )
            ).scalar_one()
        employee = replace(actor, user_id=user, membership_id=member, role=MembershipRole.EMPLOYEE)
        feed = await digests.list(employee)
        assert len(feed) == 1 and feed[0].snapshot.scope == "OWN_WORK"
        assert feed[0].report_link is None
        assert report.report.id not in [t.id for t in feed[0].snapshot.tasks]
        assert "narrative" not in feed[0].snapshot.model_dump_json()
        with pytest.raises(ReportError, match="FORBIDDEN"):
            await service.ensure_draft(
                actor=employee,
                summary_id=summary.id,
                locale="vi",
                workflow_version="reporting-narrative.v1",
            )
        with pytest.raises(ReportError, match="RESOURCE_NOT_FOUND"):
            await service.ensure_draft(
                actor=actor,
                summary_id=uuid4(),
                locale="vi",
                workflow_version="reporting-narrative.v1",
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_old_window_mode_does_not_backfill_narrative(case: Case):
    from app.modules.reporting.adapters.summary_repository import SummaryReportTransactions
    from app.modules.reporting.application.summary_report_service import SummaryReportService
    from app.modules.reporting.domain.reports import ReportError

    engine, actor, schedules, saved, window, digests = await setup(case)
    try:
        summary = await digests.trigger(
            AuthorizedJobScope(actor, datetime.now(UTC)), window.id, "CUTOFF"
        )
        _, draft, version = await schedules.prepare_from_chat(
            actor,
            ChatScheduleCommand(
                operation="CONFIGURE",
                project_reference=str(saved.project_id),
                narrative_mode="DRAFT_FOR_MANAGER",
            ),
            str(uuid4()),
        )
        assert draft is not None
        await schedules.confirm(actor, draft.id, version, str(uuid4()))
        with pytest.raises(ReportError, match="SUMMARY_NARRATIVE_CANCELLED"):
            await SummaryReportService(
                SummaryReportTransactions(async_sessionmaker(engine, expire_on_commit=False), "UTC")
            ).ensure_draft(
                actor=actor,
                summary_id=summary.id,
                locale="vi",
                workflow_version="reporting-narrative.v1",
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["creator", "pause", "recipient"])
async def test_queued_generation_rechecks_original_creator(case: Case, revocation: str):
    from pydantic import BaseModel

    from app.modules.identity.adapters.auth_repository import SqlAlchemyAuthTransactionFactory
    from app.modules.identity.adapters.current_actor import CurrentActorResolver
    from app.modules.identity.application.current_actor_service import CurrentActorService
    from app.modules.reporting.adapters.generation_repository import GenerationTransactions
    from app.modules.reporting.adapters.narrative_runtime import ReportNarrativeRuntime
    from app.modules.reporting.adapters.summary_repository import SummaryReportTransactions
    from app.modules.reporting.application.job_service import ReportJobService
    from app.modules.reporting.application.summary_report_service import SummaryReportService
    from work_management_ai.model_gateway.contracts import (
        StructuredModelRequest,
        StructuredModelResponse,
    )

    engine, actor, schedules, saved, window, digests = await setup(
        case, narrative_mode="DRAFT_FOR_MANAGER"
    )
    calls: list[str] = []

    class Never:
        async def generate_structured[T: BaseModel](
            self, request: StructuredModelRequest[T]
        ) -> StructuredModelResponse[T]:
            calls.append(request.invocation_key)
            raise AssertionError("revoked schedule must never call provider")

    try:
        summary = await digests.trigger(
            AuthorizedJobScope(actor, datetime.now(UTC)), window.id, "CUTOFF"
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        report = await SummaryReportService(
            SummaryReportTransactions(sessions, "UTC")
        ).ensure_draft(
            actor=actor,
            summary_id=summary.id,
            locale="vi",
            workflow_version="reporting-narrative.v1",
        )
        if revocation == "pause":
            await schedules.pause(actor, saved.id, saved.version, True, str(uuid4()))
        else:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE memberships SET role='EMPLOYEE' WHERE id=:id")
                    if revocation == "creator"
                    else text("UPDATE memberships SET is_active=false WHERE id=:id"),
                    {"id": actor.membership_id},
                )
        worker = ReportJobService(
            GenerationTransactions(sessions, "UTC"),
            ReportNarrativeRuntime(
                sessions=sessions,
                actors=CurrentActorResolver(
                    CurrentActorService(SqlAlchemyAuthTransactionFactory(sessions))
                ),
                gateway=Never(),
                timezone="UTC",
            ),
        )
        assert await worker.run_once(worker_id="revoked", organization_id=actor.organization_id)
        assert not calls
        async with engine.connect() as conn:
            assert (
                await conn.execute(
                    text("SELECT count(*) FROM report_versions WHERE report_id=:id"),
                    {"id": report.report.id},
                )
            ).scalar_one() == 1
            assert (
                await conn.execute(
                    text("SELECT count(*) FROM report_publications WHERE report_id=:id"),
                    {"id": report.report.id},
                )
            ).scalar_one() == 0
            assert (
                await conn.execute(
                    text("SELECT state FROM report_generation_jobs WHERE report_id=:id"),
                    {"id": report.report.id},
                )
            ).scalar_one() in {"FAILED", "AI_UNAVAILABLE"}
    finally:
        await engine.dispose()
