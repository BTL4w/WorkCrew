"""Real PostgreSQL generation, proposal boundaries and manual fallback."""

from dataclasses import replace
from typing import Literal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.reporting.adapters.generation_repository import GenerationTransactions
from app.modules.reporting.application.generation_service import GenerationService
from app.modules.reporting.domain.commands import CreateReportCommand, GenerateNarrativeCommand
from app.modules.reporting.domain.reports import ReportError
from tests.test_report_api_integration import ReportHarness, pytestmark, report_harness

__all__ = ["pytestmark", "report_harness"]


@pytest.mark.asyncio
async def test_default_create_queues_one_generation(report_harness: ReportHarness):
    h = report_harness
    key = str(uuid4())
    command = CreateReportCommand(project_id=h.project_id)
    first = await h.service.create(actor=h.actor, command=command, idempotency_key=key)
    second = await h.service.create(actor=h.actor, command=command, idempotency_key=key)
    assert first.generation_state == "QUEUED"
    assert second.generation_id == first.generation_id
    assert (
        await h.sql(
            "SELECT count(*) FROM report_generation_jobs WHERE report_id=:id",
            {"id": first.report.id},
        )
    ).scalar_one() == 1
    manual = await h.service.create(
        actor=h.actor,
        command=command.model_copy(update={"narrative_enabled": False}),
        idempotency_key=str(uuid4()),
    )
    assert manual.generation_state == "NOT_REQUESTED"
    assert manual.generation_id is None


@pytest.mark.asyncio
async def test_generate_rejects_stale_base_version(report_harness: ReportHarness):
    h = report_harness
    report = await h.service.create(
        actor=h.actor,
        command=CreateReportCommand(project_id=h.project_id, narrative_enabled=False),
        idempotency_key=str(uuid4()),
    )
    service = GenerationService(
        GenerationTransactions(async_sessionmaker(h.engine, expire_on_commit=False), "UTC")
    )
    command = GenerateNarrativeCommand(
        base_version_id=report.selected_version.id, snapshot_hash=report.snapshot.snapshot_hash
    )
    with pytest.raises(ReportError, match="STALE_REPORT_VERSION"):
        await service.request(
            actor=h.actor,
            report_id=report.report.id,
            command=command,
            expected_version=99,
            idempotency_key=str(uuid4()),
        )
    assert (
        await h.sql(
            "SELECT count(*) FROM report_generation_jobs WHERE report_id=:id",
            {"id": report.report.id},
        )
    ).scalar_one() == 0
    assert (
        await h.sql(
            "SELECT count(*) FROM audit_events WHERE organization_id=:org "
            "AND action='report.generation.requested' AND outcome='REJECTED'",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 1
    key = str(uuid4())
    first = await service.request(
        actor=h.actor,
        report_id=report.report.id,
        command=command,
        expected_version=report.report.version,
        idempotency_key=key,
    )
    replay = await service.request(
        actor=h.actor,
        report_id=report.report.id,
        command=command,
        expected_version=report.report.version,
        idempotency_key=key,
    )
    assert replay.generation_id == first.generation_id
    assert replay.replayed
    for actor in (h.employee, h.foreign):
        with pytest.raises(ReportError):
            await service.request(
                actor=actor,
                report_id=report.report.id,
                command=command,
                expected_version=report.report.version,
                idempotency_key=str(uuid4()),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["vi", "en"])
async def test_worker_hub_stores_verified_proposal(
    report_harness: ReportHarness, locale: Literal["vi", "en"]
):
    from typing import cast

    from app.core.config import Settings
    from app.modules.assistant.adapters.agent_runtime import CurrentActorResolverPort
    from app.modules.planning_runs.adapters.ai_runtime import build_model_gateway
    from app.modules.reporting.adapters.narrative_runtime import ReportNarrativeRuntime
    from app.modules.reporting.application.job_service import ReportJobService
    from app.modules.reporting.domain.commands import PublishReportCommand

    h = report_harness

    class Actors:
        async def resolve(
            self, *, organization_id: UUID, membership_id: UUID
        ) -> AuthenticatedActor | None:
            return (
                h.actor
                if organization_id == h.actor.organization_id
                and membership_id == h.actor.membership_id
                else None
            )

    result = await h.service.create(
        actor=h.actor,
        command=CreateReportCommand(project_id=h.project_id, locale=locale),
        idempotency_key=str(uuid4()),
    )
    sessions = async_sessionmaker(h.engine, expire_on_commit=False)
    jobs = ReportJobService(
        GenerationTransactions(sessions, "UTC"),
        ReportNarrativeRuntime(
            sessions=sessions,
            actors=cast(CurrentActorResolverPort, Actors()),
            gateway=build_model_gateway(Settings(environment="test", ai_provider="mock")),
            timezone="UTC",
        ),
    )
    assert await jobs.run_once(worker_id="test", organization_id=h.actor.organization_id)
    after = await h.service.get(actor=h.actor, report_id=result.report.id)
    assert after.generation_state == "AWAITING_REVIEW", (
        await h.sql(
            "SELECT state,safe_error_code FROM report_generation_jobs WHERE id=:id",
            {"id": result.generation_id},
        )
    ).all()
    assert after.selected_version.origin == "AI_PROPOSED"
    assert after.selected_version.narrative is not None
    assert after.snapshot == result.snapshot
    assert after.publications == ()
    assert after.metrics_version_id == result.selected_version.id
    assert (
        await h.sql(
            "SELECT count(*) FROM orchestration_runs WHERE report_id=:id "
            "AND trigger_kind='REPORT_REQUEST'",
            {"id": result.report.id},
        )
    ).scalar_one() == 1
    assert (
        await h.sql(
            "SELECT attempts,tools FROM report_generation_usage WHERE generation_id=:id",
            {"id": result.generation_id},
        )
    ).one() == (2, 3)
    published = await h.service.publish_metrics(
        actor=h.actor,
        report_id=result.report.id,
        command=PublishReportCommand(
            mode="METRICS_ONLY",
            report_version_id=result.selected_version.id,
            snapshot_hash=result.snapshot.snapshot_hash,
        ),
        expected_version=after.report.version,
        idempotency_key=str(uuid4()),
    )
    assert published.publications[0].report_version_id == result.selected_version.id
    assert published.selected_version.id == after.selected_version.id


@pytest.mark.asyncio
async def test_generate_api_preconditions_replay_and_tenant_denial(report_harness: ReportHarness):
    from httpx import ASGITransport, AsyncClient

    h = report_harness
    report = await h.service.create(
        actor=h.actor,
        command=CreateReportCommand(project_id=h.project_id, narrative_enabled=False),
        idempotency_key=str(uuid4()),
    )
    path = f"/api/v1/reports/{report.report.id}/generate"
    body = {
        "base_version_id": str(report.selected_version.id),
        "snapshot_hash": report.snapshot.snapshot_hash,
    }
    headers = {"Idempotency-Key": str(uuid4()), "If-Match": '"1"'}
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        missing = await client.post(path, json=body, headers={"Idempotency-Key": str(uuid4())})
        assert missing.status_code == 428
        invalid = await client.post(path, json={**body, "role": "ADMIN"}, headers=headers)
        assert invalid.status_code == 422
        first = await client.post(path, json=body, headers=headers)
        assert first.status_code == 202, first.text
        assert first.json()["generation_state"] == "QUEUED"
        replay = await client.post(path, json=body, headers=headers)
        assert (
            replay.status_code == 202
            and replay.json()["generation_id"] == first.json()["generation_id"]
        )
        assert replay.headers["Idempotency-Replayed"] == "true"
        changed = await client.post(path, json={**body, "snapshot_hash": "b" * 64}, headers=headers)
        assert changed.status_code == 409
        assert first.headers["Cache-Control"] == "private, no-store"
    for actor in (h.employee, h.foreign):
        async with AsyncClient(
            transport=ASGITransport(app=h.app(actor)), base_url="http://test"
        ) as client:
            assert (await client.post(path, json=body, headers=headers)).status_code in (403, 404)
    assert (
        await h.sql(
            "SELECT count(*) FROM audit_events WHERE organization_id=:org "
            "AND action='report.generation.requested' AND outcome='REJECTED'",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["invalid", "timeout", "semantic", "revoke", "excess_usage"])
async def test_generation_failure_preserves_metrics_and_explicit_retry(
    report_harness: ReportHarness, failure: str
):
    from pydantic import BaseModel

    from app.core.config import Settings
    from app.modules.planning_runs.adapters.ai_runtime import build_model_gateway
    from app.modules.reporting.adapters.narrative_runtime import ReportNarrativeRuntime
    from app.modules.reporting.application.job_service import ReportJobService
    from app.modules.reporting.domain.commands import PublishReportCommand
    from work_management_ai.model_gateway.contracts import (
        ModelUsage,
        StructuredModelRequest,
        StructuredModelResponse,
    )
    from work_management_ai.model_gateway.errors import ModelInvalidOutputError, ModelTimeoutError

    h = report_harness

    class Actors:
        async def resolve(
            self, *, organization_id: UUID, membership_id: UUID
        ) -> AuthenticatedActor | None:
            return (
                h.actor
                if organization_id == h.actor.organization_id
                and membership_id == h.actor.membership_id
                else None
            )

    class FailingGateway:
        calls = 0

        async def generate_structured[T: BaseModel](
            self, request: StructuredModelRequest[T]
        ) -> StructuredModelResponse[T]:
            self.calls += 1
            if failure == "invalid":
                raise ModelInvalidOutputError("unsafe provider output must not be exposed")
            if failure == "timeout":
                raise ModelTimeoutError("timeout")
            response = await build_model_gateway(
                Settings(environment="test", ai_provider="mock")
            ).generate_structured(request)
            if failure == "semantic" and request.invocation_key.endswith(".grounding"):
                return replace(
                    response,
                    parsed=request.output_schema.model_validate(
                        {"passed": False, "claim_verdicts": [], "safe_codes": ["REJECTED"]}
                    ),
                )
            if failure == "revoke" and request.invocation_key.endswith(".grounding"):
                await h.sql(
                    "UPDATE memberships SET role='EMPLOYEE' WHERE id=:id",
                    {"id": h.actor.membership_id},
                )
            if failure == "excess_usage":
                return replace(response, usage=ModelUsage(input_tokens=50000, output_tokens=9000))
            return response

    report = await h.service.create(
        actor=h.actor,
        command=CreateReportCommand(project_id=h.project_id),
        idempotency_key=str(uuid4()),
    )
    sessions = async_sessionmaker(h.engine, expire_on_commit=False)
    gateway = FailingGateway()
    jobs = ReportJobService(
        GenerationTransactions(sessions, "UTC"),
        ReportNarrativeRuntime(sessions=sessions, actors=Actors(), gateway=gateway, timezone="UTC"),
    )
    assert await jobs.run_once(worker_id="fail", organization_id=h.actor.organization_id)
    if failure == "revoke":
        await h.sql(
            "UPDATE memberships SET role='MANAGER' WHERE id=:id", {"id": h.actor.membership_id}
        )
    after = await h.service.get(actor=h.actor, report_id=report.report.id)
    assert after.generation_state == ("FAILED" if failure == "revoke" else "AI_UNAVAILABLE")
    assert after.selected_version == report.selected_version
    assert after.snapshot == report.snapshot
    assert gateway.calls <= 2
    assert (
        await h.sql(
            "SELECT count(*) FROM report_versions WHERE report_id=:id AND origin='AI_PROPOSED'",
            {"id": report.report.id},
        )
    ).scalar_one() == 0
    queued = await GenerationService(GenerationTransactions(sessions, "UTC")).request(
        actor=h.actor,
        report_id=report.report.id,
        command=GenerateNarrativeCommand(
            base_version_id=report.selected_version.id, snapshot_hash=report.snapshot.snapshot_hash
        ),
        expected_version=after.report.version,
        idempotency_key=str(uuid4()),
    )
    assert queued.generation_id != after.generation_id and queued.snapshot == after.snapshot
    published = await h.service.publish_metrics(
        actor=h.actor,
        report_id=report.report.id,
        command=PublishReportCommand(
            mode="METRICS_ONLY",
            report_version_id=report.selected_version.id,
            snapshot_hash=report.snapshot.snapshot_hash,
        ),
        expected_version=queued.report.version,
        idempotency_key=str(uuid4()),
    )
    assert published.publications[0].report_version_id == report.selected_version.id


@pytest.mark.asyncio
async def test_generation_rls_and_cross_tenant_references(report_harness: ReportHarness):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    h = report_harness
    result = await h.service.create(
        actor=h.actor,
        command=CreateReportCommand(project_id=h.project_id),
        idempotency_key=str(uuid4()),
    )
    for actor in (h.employee, h.foreign):
        async with h.engine.connect() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true),"
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            for table in ("report_generation_jobs", "report_generation_usage"):
                assert (
                    await session.execute(text(f"SELECT count(*) FROM {table}"))
                ).scalar_one() == 0
    with pytest.raises(DBAPIError) as foreign_reference:
        await h.sql(
            "UPDATE report_generation_jobs SET requester_membership_id=:foreign WHERE id=:id",
            {"foreign": h.foreign.membership_id, "id": result.generation_id},
        )
    assert getattr(foreign_reference.value.orig, "sqlstate", None) == "23503"


@pytest.mark.asyncio
@pytest.mark.parametrize("also_fail_grounding", [False, True])
async def test_durable_transient_retry_is_shared_and_reserved_before_provider(
    report_harness: ReportHarness, also_fail_grounding: bool
):
    from pydantic import BaseModel

    from app.core.config import Settings
    from app.modules.planning_runs.adapters.ai_runtime import build_model_gateway
    from app.modules.reporting.adapters.narrative_runtime import ReportNarrativeRuntime
    from app.modules.reporting.application.job_service import ReportJobService
    from work_management_ai.model_gateway.contracts import (
        StructuredModelRequest,
        StructuredModelResponse,
    )
    from work_management_ai.model_gateway.errors import ModelTimeoutError

    h = report_harness
    report = await h.service.create(
        actor=h.actor,
        command=CreateReportCommand(project_id=h.project_id),
        idempotency_key=str(uuid4()),
    )

    class Actors:
        async def resolve(
            self, *, organization_id: UUID, membership_id: UUID
        ) -> AuthenticatedActor | None:
            return (
                h.actor
                if organization_id == h.actor.organization_id
                and membership_id == h.actor.membership_id
                else None
            )

    class RetryGateway:
        calls = 0

        async def generate_structured[T: BaseModel](
            self, request: StructuredModelRequest[T]
        ) -> StructuredModelResponse[T]:
            self.calls += 1
            usage = (
                await h.sql(
                    "SELECT attempts,input_reserved,output_reserved FROM report_generation_usage "
                    "WHERE generation_id=:id",
                    {"id": report.generation_id},
                )
            ).one()
            assert usage.attempts == self.calls and usage.input_reserved > 0
            # The reservation transaction has committed before provider execution.
            await h.sql(
                "UPDATE report_generation_usage SET reservations=reservations "
                "WHERE generation_id=:id",
                {"id": report.generation_id},
            )
            assert request.max_output_tokens == (
                1000 if request.invocation_key.endswith(".grounding") else 3000
            )
            if self.calls == 1 or (
                also_fail_grounding and request.invocation_key.endswith(".grounding")
            ):
                raise ModelTimeoutError("transient")
            return await build_model_gateway(
                Settings(environment="test", ai_provider="mock")
            ).generate_structured(request)

    sessions = async_sessionmaker(h.engine, expire_on_commit=False)
    gateway = RetryGateway()
    assert await ReportJobService(
        GenerationTransactions(sessions, "UTC"),
        ReportNarrativeRuntime(sessions=sessions, actors=Actors(), gateway=gateway, timezone="UTC"),
    ).run_once(worker_id="retry", organization_id=h.actor.organization_id)
    after = await h.service.get(actor=h.actor, report_id=report.report.id)
    assert gateway.calls == 3, (
        await h.sql(
            "SELECT attempts,input_reserved,output_reserved FROM report_generation_usage "
            "WHERE generation_id=:id",
            {"id": report.generation_id},
        )
    ).all()
    assert after.generation_state == (
        "AI_UNAVAILABLE" if also_fail_grounding else "AWAITING_REVIEW"
    )
    assert (
        await h.sql(
            "SELECT attempts,output_reserved,retries FROM report_generation_usage "
            "WHERE generation_id=:id",
            {"id": report.generation_id},
        )
    ).one() == (3, 7000, 1)
    assert (
        await h.sql(
            "SELECT count(*) FROM agent_model_invocations WHERE organization_id=:org",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 3


@pytest.mark.asyncio
async def test_report_outbox_events_acknowledge_envelope_identity(report_harness: ReportHarness):
    from app.modules.planning_runs.adapters.transaction import (
        PostgreSQLPlanningRunTransactionFactory,
    )
    from app.modules.planning_runs.application.outbox_service import OutboxService
    from app.modules.planning_runs.domain.models import OutboxEvent
    from app.modules.reporting.adapters.outbox_consumer import ReportingOutboxPublisher
    from app.modules.risk.adapters.outbox_consumer import RiskOutboxPublisher

    h = report_harness
    await h.service.create(
        actor=h.actor,
        command=CreateReportCommand(project_id=h.project_id),
        idempotency_key=str(uuid4()),
    )

    class RiskRunner:
        async def queue_event(self, event: OutboxEvent) -> None:
            raise AssertionError("Report fact cannot queue risk jobs")

        async def deliver(self, event: OutboxEvent) -> None:
            raise AssertionError("Report fact cannot deliver risk alerts")

    publisher = ReportingOutboxPublisher(RiskOutboxPublisher(RiskRunner()))
    outbox = OutboxService(
        transaction_factory=PostgreSQLPlanningRunTransactionFactory(
            async_sessionmaker(h.engine, expire_on_commit=False)
        ),
        publisher=publisher,
        organization_scopes={h.actor.organization_id},
    )
    assert await outbox.dispatch_once("report-test", h.actor.organization_id)
    assert (
        await h.sql(
            "SELECT count(*) FROM outbox_events WHERE organization_id=:org AND status='DISPATCHED'",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 2
