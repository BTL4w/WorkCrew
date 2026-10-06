"""Exact version review, immutable edits and atomic terminal outcomes under real RLS."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.planning_runs.adapters.ai_runtime import build_model_gateway
from app.modules.reporting.adapters.generation_repository import GenerationTransactions
from app.modules.reporting.adapters.narrative_runtime import ReportNarrativeRuntime
from app.modules.reporting.adapters.review_repository import ReviewTransactions
from app.modules.reporting.application.job_service import ReportJobService
from app.modules.reporting.application.review_service import ReviewService
from app.modules.reporting.domain.commands import (
    CreateReportCommand,
    EditReportCommand,
    PublishReportCommand,
    RejectReportCommand,
)
from app.modules.reporting.domain.reports import ReportError, ReportResult
from tests.test_report_api_integration import ReportHarness, pytestmark, report_harness
from work_management_ai.model_gateway.contracts import ModelGateway

__all__ = ["pytestmark", "report_harness"]


def reviews(h: ReportHarness):
    return ReviewService(
        ReviewTransactions(async_sessionmaker(h.engine, expire_on_commit=False), "UTC")
    )


async def worker(h: ReportHarness, gateway: ModelGateway | None = None):
    class Actors:
        async def resolve(
            self, *, organization_id: UUID, membership_id: UUID
        ) -> AuthenticatedActor | None:
            return (
                h.actor
                if (organization_id, membership_id)
                == (h.actor.organization_id, h.actor.membership_id)
                else None
            )

    sessions = async_sessionmaker(h.engine, expire_on_commit=False)
    return await ReportJobService(
        GenerationTransactions(sessions, "UTC"),
        ReportNarrativeRuntime(
            sessions=sessions,
            actors=Actors(),
            gateway=gateway
            or build_model_gateway(Settings(environment="test", ai_provider="mock")),
            timezone="UTC",
        ),
    ).run_once(worker_id="review-test", organization_id=h.actor.organization_id)


async def ready(h: ReportHarness):
    report = await h.service.create(
        actor=h.actor,
        command=CreateReportCommand(project_id=h.project_id, locale="en"),
        idempotency_key=str(uuid4()),
    )
    assert await worker(h)
    return await h.service.get(actor=h.actor, report_id=report.report.id)


def publish_command(r: ReportResult):
    return PublishReportCommand(
        mode="REVIEWED_NARRATIVE",
        report_version_id=r.selected_version.id,
        snapshot_hash=r.snapshot.snapshot_hash,
    )


@pytest.mark.asyncio
async def test_unedited_publish_records_accept(report_harness: ReportHarness):
    h = report_harness
    r = await ready(h)
    key = str(uuid4())
    result = await reviews(h).publish(
        actor=h.actor,
        report_id=r.report.id,
        command=publish_command(r),
        expected_version=r.report.version,
        idempotency_key=key,
    )
    assert result.report_result.publications[0].report_version_id == r.selected_version.id
    assert (
        await h.sql(
            "SELECT decision FROM feedback WHERE id=:id", {"id": result.terminal_outcome_id}
        )
    ).scalar_one() == "ACCEPT"
    replay = await reviews(h).publish(
        actor=h.actor,
        report_id=r.report.id,
        command=publish_command(r),
        expected_version=r.report.version,
        idempotency_key=key,
    )
    assert replay.terminal_outcome_id == result.terminal_outcome_id
    assert replay.report_result.replayed
    assert (
        await h.sql("SELECT count(*) FROM feedback WHERE report_id=:id", {"id": r.report.id})
    ).scalar_one() == 1


@pytest.mark.asyncio
async def test_edited_publish_records_edit_for_original(report_harness: ReportHarness):
    h = report_harness
    r = await ready(h)
    assert r.selected_version.narrative
    blocks = list(r.selected_version.narrative.blocks)
    from work_management_ai.agents.reporting.contracts import NarrativeTextBlock, SourceIdentity

    blocks.append(
        NarrativeTextBlock(
            id="advice",
            section="recommendations",
            kind="RECOMMENDATION",
            text="Review the captured work before deciding next steps.",
            source_refs=(
                SourceIdentity.model_validate(
                    r.snapshot.sources[0].model_dump(
                        include={"resource_type", "resource_id", "version", "fingerprint"}
                    )
                ),
            ),
            assumptions=("Team review is available",),
        )
    )
    narrative = r.selected_version.narrative.model_copy(update={"blocks": tuple(blocks)})
    edited = await reviews(h).edit(
        actor=h.actor,
        report_id=r.report.id,
        command=EditReportCommand(
            parent_version_id=r.selected_version.id,
            snapshot_hash=r.snapshot.snapshot_hash,
            narrative=narrative,
        ),
        expected_version=r.report.version,
        idempotency_key=str(uuid4()),
    )
    assert edited.selected_version.origin == "AI_EDITED"
    assert edited.selected_version.generation_id == r.generation_id
    assert edited.verification_state == "PENDING"
    assert edited.publications == ()
    assert (
        await h.sql("SELECT count(*) FROM feedback WHERE report_id=:id", {"id": r.report.id})
    ).scalar_one() == 0
    with pytest.raises(ReportError, match="REPORT_VERIFICATION_REQUIRED"):
        await reviews(h).publish(
            actor=h.actor,
            report_id=r.report.id,
            command=publish_command(edited),
            expected_version=edited.report.version,
            idempotency_key=str(uuid4()),
        )
    assert await worker(h)
    checked = await h.service.get(actor=h.actor, report_id=r.report.id)
    assert checked.verification_state == "VERIFIED"
    assert checked.selected_version == edited.selected_version
    result = await reviews(h).publish(
        actor=h.actor,
        report_id=r.report.id,
        command=publish_command(checked),
        expected_version=checked.report.version,
        idempotency_key=str(uuid4()),
    )
    outcome = (
        await h.sql(
            "SELECT decision,generation_id,original_version_id,report_version_id "
            "FROM feedback WHERE id=:id",
            {"id": result.terminal_outcome_id},
        )
    ).one()
    assert outcome == ("EDIT", r.generation_id, r.selected_version.id, edited.selected_version.id)
    assert (
        await h.sql(
            "SELECT kind FROM report_review_decisions WHERE id=:id", {"id": result.decision_id}
        )
    ).scalar_one() == "ACCEPT"
    assert (
        await h.sql(
            "SELECT attempts FROM report_generation_usage WHERE generation_id=:id",
            {"id": checked.generation_id},
        )
    ).scalar_one() == 1


@pytest.mark.asyncio
async def test_reject_has_no_publication(report_harness: ReportHarness):
    h = report_harness
    r = await ready(h)
    cmd = RejectReportCommand(
        report_version_id=r.selected_version.id,
        snapshot_hash=r.snapshot.snapshot_hash,
        reason="Needs revision",
    )
    result = await reviews(h).reject(
        actor=h.actor,
        report_id=r.report.id,
        command=cmd,
        expected_version=r.report.version,
        idempotency_key=str(uuid4()),
    )
    assert result.report_result.publications == ()
    assert result.report_result.review_state == "REJECTED"
    with pytest.raises(ReportError, match="REPORT_ALREADY_REVIEWED"):
        await reviews(h).publish(
            actor=h.actor,
            report_id=r.report.id,
            command=publish_command(r),
            expected_version=result.report_result.report.version,
            idempotency_key=str(uuid4()),
        )


@pytest.mark.asyncio
async def test_stale_review_and_revoke_have_no_side_effects(report_harness: ReportHarness):
    h = report_harness
    r = await ready(h)
    for actor, version, code in [
        (h.actor, 999, "STALE_REPORT_VERSION"),
        (h.employee, r.report.version, "FORBIDDEN"),
        (h.foreign, r.report.version, "RESOURCE_NOT_FOUND"),
    ]:
        with pytest.raises(ReportError, match=code):
            await reviews(h).publish(
                actor=actor,
                report_id=r.report.id,
                command=publish_command(r),
                expected_version=version,
                idempotency_key=str(uuid4()),
            )
    await h.sql(
        "UPDATE memberships SET role='EMPLOYEE' WHERE id=:id", {"id": h.actor.membership_id}
    )
    with pytest.raises(ReportError, match="FORBIDDEN"):
        await reviews(h).publish(
            actor=h.actor,
            report_id=r.report.id,
            command=publish_command(r),
            expected_version=r.report.version,
            idempotency_key=str(uuid4()),
        )
    assert (
        await h.sql(
            "SELECT count(*) FROM report_publications WHERE report_id=:id", {"id": r.report.id}
        )
    ).scalar_one() == 0
    assert (
        await h.sql("SELECT count(*) FROM feedback WHERE report_id=:id", {"id": r.report.id})
    ).scalar_one() == 0
    assert (
        await h.sql(
            "SELECT count(*) FROM audit_events WHERE resource_id=:id AND outcome='REJECTED'",
            {"id": r.report.id},
        )
    ).scalar_one() == 4


@pytest.mark.asyncio
async def test_metrics_only_is_not_ai_acceptance(report_harness: ReportHarness):
    h = report_harness
    r = await ready(h)
    assert r.metrics_version_id is not None
    result = await h.service.publish_metrics(
        actor=h.actor,
        report_id=r.report.id,
        command=PublishReportCommand(
            mode="METRICS_ONLY",
            report_version_id=r.metrics_version_id,
            snapshot_hash=r.snapshot.snapshot_hash,
        ),
        expected_version=r.report.version,
        idempotency_key=str(uuid4()),
    )
    assert result.publications
    assert (
        await h.sql("SELECT count(*) FROM feedback WHERE report_id=:id", {"id": r.report.id})
    ).scalar_one() == 0


@pytest.mark.asyncio
async def test_ai_lineage_cannot_be_spoofed(report_harness: ReportHarness):
    from httpx import ASGITransport, AsyncClient

    h = report_harness
    r = await ready(h)
    assert r.selected_version.narrative is not None
    doc = r.selected_version.narrative.model_dump(mode="json")
    doc["blocks"][0]["bindings"][0]["value"] = "999"
    headers = {"Idempotency-Key": str(uuid4()), "If-Match": f'"{r.report.version}"'}
    path = f"/api/v1/reports/{r.report.id}"
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        for extra in (
            {"origin": "HUMAN"},
            {"generation_id": str(uuid4())},
            {"approved": True},
            {"block_origins": {"human_advice": "AI"}},
        ):
            response = await client.post(
                path + "/versions",
                json={
                    "parent_version_id": str(r.selected_version.id),
                    "snapshot_hash": r.snapshot.snapshot_hash,
                    "narrative": r.selected_version.narrative.model_dump(mode="json"),
                    **extra,
                },
                headers=headers,
            )
            assert response.status_code == 422
        response = await client.post(
            path + "/versions",
            json={
                "parent_version_id": str(r.selected_version.id),
                "snapshot_hash": r.snapshot.snapshot_hash,
                "narrative": doc,
            },
            headers=headers,
        )
        assert response.status_code == 409
        for endpoint, body in [
            (
                "/versions",
                {
                    "parent_version_id": str(r.selected_version.id),
                    "snapshot_hash": r.snapshot.snapshot_hash,
                    "narrative": r.selected_version.narrative.model_dump(mode="json"),
                },
            ),
            (
                "/review-decisions",
                {
                    "report_version_id": str(r.selected_version.id),
                    "snapshot_hash": r.snapshot.snapshot_hash,
                    "reason": "Needs review",
                },
            ),
        ]:
            assert (
                await client.post(
                    path + endpoint, json=body, headers={"Idempotency-Key": str(uuid4())}
                )
            ).status_code == 428
            assert (
                await client.post(path + endpoint, json=body, headers={**headers, "If-Match": "1"})
            ).status_code == 400
    assert (
        await h.sql("SELECT count(*) FROM report_versions WHERE report_id=:id", {"id": r.report.id})
    ).scalar_one() == 2
    assert (
        await h.sql("SELECT count(*) FROM feedback WHERE report_id=:id", {"id": r.report.id})
    ).scalar_one() == 0


@pytest.mark.asyncio
async def test_review_rolls_back_if_feedback_fails(
    report_harness: ReportHarness, monkeypatch: pytest.MonkeyPatch
):
    from app.modules.feedback.adapters.repository import SQLFeedbackRepository

    h = report_harness
    r = await ready(h)

    async def fail(*args: object, **kwargs: object):
        raise ReportError("FEEDBACK_UNAVAILABLE")

    monkeypatch.setattr(SQLFeedbackRepository, "append", fail)
    with pytest.raises(ReportError, match="FEEDBACK_UNAVAILABLE"):
        await reviews(h).publish(
            actor=h.actor,
            report_id=r.report.id,
            command=publish_command(r),
            expected_version=r.report.version,
            idempotency_key=str(uuid4()),
        )
    assert (
        await h.service.get(actor=h.actor, report_id=r.report.id)
    ).report.version == r.report.version
    for table in ("feedback", "report_publications", "report_review_decisions"):
        assert (
            await h.sql(f"SELECT count(*) FROM {table} WHERE report_id=:id", {"id": r.report.id})
        ).scalar_one() == 0
    assert (
        await h.sql(
            "SELECT count(*) FROM outbox_events WHERE aggregate_id=:id "
            "AND event_type='report.published.v1'",
            {"id": r.report.id},
        )
    ).scalar_one() == 0


@pytest.mark.asyncio
async def test_edit_and_reject_replay_and_changed_payload(report_harness: ReportHarness):
    h = report_harness
    r = await ready(h)
    assert r.selected_version.narrative is not None
    key = str(uuid4())
    cmd = EditReportCommand(
        parent_version_id=r.selected_version.id,
        snapshot_hash=r.snapshot.snapshot_hash,
        narrative=r.selected_version.narrative,
    )
    edited = await reviews(h).edit(
        actor=h.actor,
        report_id=r.report.id,
        command=cmd,
        expected_version=r.report.version,
        idempotency_key=key,
    )
    replay = await reviews(h).edit(
        actor=h.actor,
        report_id=r.report.id,
        command=cmd,
        expected_version=r.report.version,
        idempotency_key=key,
    )
    assert replay.selected_version.id == edited.selected_version.id and replay.replayed
    with pytest.raises(ReportError, match="IDEMPOTENCY_KEY_REUSED"):
        await reviews(h).edit(
            actor=h.actor,
            report_id=r.report.id,
            command=cmd.model_copy(update={"snapshot_hash": "b" * 64}),
            expected_version=r.report.version,
            idempotency_key=key,
        )
    reject = RejectReportCommand(
        report_version_id=edited.selected_version.id,
        snapshot_hash=r.snapshot.snapshot_hash,
        reason="Incorrect context",
    )
    key = str(uuid4())
    first = await reviews(h).reject(
        actor=h.actor,
        report_id=r.report.id,
        command=reject,
        expected_version=edited.report.version,
        idempotency_key=key,
    )
    second = await reviews(h).reject(
        actor=h.actor,
        report_id=r.report.id,
        command=reject,
        expected_version=edited.report.version,
        idempotency_key=key,
    )
    assert second.terminal_outcome_id == first.terminal_outcome_id
    assert await worker(h)
    assert (
        await h.service.get(actor=h.actor, report_id=r.report.id)
    ).verification_state == "FAILED"
    with pytest.raises(ReportError, match="REPORT_ALREADY_REVIEWED"):
        await reviews(h).edit(
            actor=h.actor,
            report_id=r.report.id,
            command=cmd.model_copy(update={"parent_version_id": edited.selected_version.id}),
            expected_version=first.report_result.report.version,
            idempotency_key=str(uuid4()),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "invalid", "semantic", "revoke"])
async def test_edit_verification_failure_preserves_manual_fallback(
    report_harness: ReportHarness, failure: str
):
    from dataclasses import replace

    from pydantic import BaseModel

    from app.modules.reporting.domain.commands import ReportEditVerificationCommand
    from work_management_ai.model_gateway.contracts import (
        StructuredModelRequest,
        StructuredModelResponse,
    )
    from work_management_ai.model_gateway.errors import ModelInvalidOutputError, ModelTimeoutError

    h = report_harness
    r = await ready(h)
    assert r.selected_version.narrative is not None
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

    class Failure:
        async def generate_structured[T: BaseModel](
            self, request: StructuredModelRequest[T]
        ) -> StructuredModelResponse[T]:
            if failure == "timeout":
                raise ModelTimeoutError("private provider error")
            if failure == "invalid":
                raise ModelInvalidOutputError("private invalid output")
            response = await build_model_gateway(
                Settings(environment="test", ai_provider="mock")
            ).generate_structured(request)
            if failure == "semantic":
                return replace(
                    response,
                    parsed=request.output_schema.model_validate(
                        {"passed": False, "claim_verdicts": [], "safe_codes": ["REJECTED"]}
                    ),
                )
            await h.sql(
                "UPDATE memberships SET role='EMPLOYEE' WHERE id=:id", {"id": h.actor.membership_id}
            )
            return response

    assert await worker(h, Failure())
    if failure == "revoke":
        await h.sql(
            "UPDATE memberships SET role='MANAGER' WHERE id=:id", {"id": h.actor.membership_id}
        )
    checked = await h.service.get(actor=h.actor, report_id=r.report.id)
    assert checked.verification_state == "FAILED"
    assert checked.selected_version == edited.selected_version
    with pytest.raises(ReportError, match="REPORT_VERIFICATION_REQUIRED"):
        await reviews(h).publish(
            actor=h.actor,
            report_id=r.report.id,
            command=publish_command(checked),
            expected_version=checked.report.version,
            idempotency_key=str(uuid4()),
        )
    assert r.metrics_version_id is not None
    manual = await h.service.publish_metrics(
        actor=h.actor,
        report_id=r.report.id,
        command=PublishReportCommand(
            mode="METRICS_ONLY",
            report_version_id=r.metrics_version_id,
            snapshot_hash=r.snapshot.snapshot_hash,
        ),
        expected_version=checked.report.version,
        idempotency_key=str(uuid4()),
    )
    assert manual.publications[0].report_version_id == r.metrics_version_id
    assert (
        await h.sql(
            "SELECT count(*) FROM feedback WHERE report_id=:id AND kind='TERMINAL_QUALITY'",
            {"id": r.report.id},
        )
    ).scalar_one() == 0
    assert r.generation_id is not None
    retry = await reviews(h).request_verification(
        actor=h.actor,
        command=ReportEditVerificationCommand(
            report_id=r.report.id,
            edited_version_id=edited.selected_version.id,
            parent_version_id=r.selected_version.id,
            snapshot_hash=r.snapshot.snapshot_hash,
            original_generation_id=r.generation_id,
        ),
        expected_version=manual.report.version,
        idempotency_key=str(uuid4()),
    )
    assert retry.selected_version == edited.selected_version
    assert await worker(h)
    after = await h.service.get(actor=h.actor, report_id=r.report.id)
    assert after.verification_state == "VERIFIED"
    assert after.selected_version.generation_id == r.generation_id


@pytest.mark.asyncio
@pytest.mark.parametrize("first_mode", ["METRICS_ONLY", "REVIEWED_NARRATIVE"])
async def test_publication_key_is_shared_across_modes(
    report_harness: ReportHarness, first_mode: str
):
    from httpx import ASGITransport, AsyncClient

    h = report_harness
    r = await ready(h)
    key = str(uuid4())
    body = {
        "mode": first_mode,
        "report_version_id": str(
            r.metrics_version_id if first_mode == "METRICS_ONLY" else r.selected_version.id
        ),
        "snapshot_hash": r.snapshot.snapshot_hash,
    }
    path = f"/api/v1/reports/{r.report.id}/publish"
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        first = await client.post(
            path, json=body, headers={"Idempotency-Key": key, "If-Match": f'"{r.report.version}"'}
        )
        assert first.status_code == 201, first.text
        opposite = {
            **body,
            "mode": "REVIEWED_NARRATIVE" if first_mode == "METRICS_ONLY" else "METRICS_ONLY",
            "report_version_id": str(
                r.selected_version.id if first_mode == "METRICS_ONLY" else r.metrics_version_id
            ),
        }
        second = await client.post(
            path,
            json=opposite,
            headers={"Idempotency-Key": key, "If-Match": f'"{first.json()["report"]["version"]}"'},
        )
        assert second.status_code == 409, second.text
        assert second.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert (
        await h.sql(
            "SELECT count(*) FROM report_publications WHERE report_id=:id", {"id": r.report.id}
        )
    ).scalar_one() == 1


@pytest.mark.asyncio
async def test_unavailable_narrative_sources_block_release_and_hide_history(
    report_harness: ReportHarness, monkeypatch: pytest.MonkeyPatch
):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.modules.identity.domain.auth import AuthenticatedActor
    from app.modules.reporting.adapters import source_reader
    from app.modules.reporting.adapters.source_reader import Freshness
    from app.modules.reporting.domain.metrics import SourceRef
    from work_management_ai.agents.reporting.contracts import NarrativeTextBlock, SourceIdentity

    h = report_harness
    r = await ready(h)
    assert r.selected_version.narrative is not None
    source = next(s for s in r.snapshot.sources if s.resource_type == "TASK")
    note = NarrativeTextBlock(
        id="human_advice",
        section="recommendations",
        kind="RECOMMENDATION",
        text="Review captured work with the team.",
        source_refs=(
            SourceIdentity.model_validate(
                source.model_dump(
                    include={"resource_type", "resource_id", "version", "fingerprint"}
                )
            ),
        ),
        assumptions=("A team review is available",),
    )
    doc = r.selected_version.narrative.model_copy(
        update={"blocks": (*r.selected_version.narrative.blocks, note)}
    )
    edited = await reviews(h).edit(
        actor=h.actor,
        report_id=r.report.id,
        command=EditReportCommand(
            parent_version_id=r.selected_version.id,
            snapshot_hash=r.snapshot.snapshot_hash,
            narrative=doc,
        ),
        expected_version=r.report.version,
        idempotency_key=str(uuid4()),
    )
    assert edited.selected_version.block_origins["human_advice"] == "HUMAN"
    assert await worker(h)
    checked = await h.service.get(actor=h.actor, report_id=r.report.id)
    original = source_reader.source_freshness

    async def unavailable(
        session: AsyncSession, actor: AuthenticatedActor, ref: SourceRef
    ) -> Freshness:
        return (
            "UNAVAILABLE"
            if ref.resource_id == source.resource_id
            else await original(session, actor, ref)
        )

    monkeypatch.setattr(source_reader, "source_freshness", unavailable)
    with pytest.raises(ReportError, match="REPORT_SOURCE_UNAVAILABLE"):
        await reviews(h).publish(
            actor=h.actor,
            report_id=r.report.id,
            command=publish_command(checked),
            expected_version=checked.report.version,
            idempotency_key=str(uuid4()),
        )
    redacted = await h.service.get(actor=h.actor, report_id=r.report.id)
    assert redacted.narrative_access_state == "UNAVAILABLE"
    assert redacted.selected_version.narrative is None
    assert redacted.snapshot == r.snapshot
    monkeypatch.setattr(source_reader, "source_freshness", original)
    publication_key = str(uuid4())
    published = await reviews(h).publish(
        actor=h.actor,
        report_id=r.report.id,
        command=publish_command(checked),
        expected_version=checked.report.version,
        idempotency_key=publication_key,
    )
    assert published.report_result.published_versions[0].narrative == doc
    monkeypatch.setattr(source_reader, "source_freshness", unavailable)
    hidden = await h.service.get(actor=h.actor, report_id=r.report.id)
    assert hidden.published_versions[0].narrative is None
    assert hidden.snapshot == r.snapshot
    replay = await reviews(h).publish(
        actor=h.actor,
        report_id=r.report.id,
        command=publish_command(checked),
        expected_version=checked.report.version,
        idempotency_key=publication_key,
    )
    assert replay.report_result.replayed
    assert replay.report_result.selected_version.narrative is None
    assert replay.report_result.published_versions[0].narrative is None


@pytest.mark.asyncio
async def test_unavailable_provider_context_falls_back_before_model(
    report_harness: ReportHarness, monkeypatch: pytest.MonkeyPatch
):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.modules.reporting.adapters import source_reader
    from app.modules.reporting.adapters.source_reader import Freshness
    from app.modules.reporting.domain.metrics import SourceRef

    h = report_harness
    r = await h.service.create(
        actor=h.actor,
        command=CreateReportCommand.model_validate(h.body()),
        idempotency_key=str(uuid4()),
    )

    async def unavailable(
        session: AsyncSession, actor: AuthenticatedActor, ref: SourceRef
    ) -> Freshness:
        return "UNAVAILABLE"

    monkeypatch.setattr(source_reader, "source_freshness", unavailable)
    assert await worker(h)
    result = await h.service.get(actor=h.actor, report_id=r.report.id)
    assert result.generation_state in ("FAILED", "AI_UNAVAILABLE")
    assert result.selected_version.origin == "METRICS_ONLY"
    assert (
        await h.sql(
            "SELECT attempts FROM report_generation_usage WHERE generation_id=:id",
            {"id": r.generation_id},
        )
    ).scalar_one() == 0
