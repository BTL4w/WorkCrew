"""Fenced leases and durable counters use the actual SQL store."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.reporting.adapters.generation_repository import GenerationTransactions
from app.modules.reporting.adapters.usage import ReportingUsageStore
from app.modules.reporting.application.job_service import ReportJobService
from app.modules.reporting.domain.commands import CreateReportCommand
from tests.test_report_api_integration import ReportHarness, pytestmark, report_harness
from work_management_ai.agents.reporting.contracts import ReportingUsageScope

__all__ = ["pytestmark", "report_harness"]


@pytest.mark.asyncio
async def test_durable_budget_survives_claims(report_harness: ReportHarness):
    h = report_harness
    report = await h.service.create(
        actor=h.actor,
        command=CreateReportCommand(project_id=h.project_id),
        idempotency_key=str(uuid4()),
    )
    sessions = async_sessionmaker(h.engine, expire_on_commit=False)
    jobs = ReportJobService(GenerationTransactions(sessions, "UTC"))
    job = await jobs.claim(worker_id="first", organization_id=h.actor.organization_id)
    assert job and job.id == report.generation_id
    scope = ReportingUsageScope(
        organization_id=h.actor.organization_id,
        membership_id=h.actor.membership_id,
        generation_id=job.id,
        fence=job.fence,
        worker_id="first",
    )
    store = ReportingUsageStore(sessions)
    assert await store.reserve(scope, input_tokens=1000, output_tokens=3000) == 1
    assert await store.reserve(scope, input_tokens=1000, output_tokens=1000) == 2
    await h.sql(
        "UPDATE report_generation_jobs SET lease_until=now()-interval '1 second' WHERE id=:id",
        {"id": job.id},
    )
    replay = await jobs.claim(worker_id="second", organization_id=h.actor.organization_id)
    assert replay and replay.id == job.id and replay.fence > job.fence
    restarted = ReportingUsageStore(sessions)
    state = await restarted.load(
        scope.model_copy(update={"fence": replay.fence, "worker_id": "second"})
    )
    assert state.attempts == 2
    assert state.output_reserved == 4000
    with pytest.raises(ValueError):
        await store.reserve(scope, input_tokens=1, output_tokens=1)


@pytest.mark.asyncio
async def test_attempt_tool_retry_and_deadline_limits(report_harness: ReportHarness):
    h = report_harness
    report = await h.service.create(
        actor=h.actor,
        command=CreateReportCommand(project_id=h.project_id),
        idempotency_key=str(uuid4()),
    )
    sessions = async_sessionmaker(h.engine, expire_on_commit=False)
    jobs = ReportJobService(GenerationTransactions(sessions, "UTC"))
    job = await jobs.claim(worker_id="one", organization_id=h.actor.organization_id)
    assert job
    scope = ReportingUsageScope(
        organization_id=h.actor.organization_id,
        membership_id=h.actor.membership_id,
        generation_id=job.id,
        fence=job.fence,
        worker_id="one",
    )
    store = ReportingUsageStore(sessions)
    for _ in range(3):
        await store.reserve(scope, input_tokens=1000, output_tokens=1000)
    with pytest.raises(ValueError, match="MODEL_BUDGET"):
        await store.reserve(scope, input_tokens=1, output_tokens=1)
    for number in range(6):
        await store.reserve_tool(scope, str(number))
    with pytest.raises(ValueError, match="TOOL_BUDGET"):
        await store.reserve_tool(scope, "seventh")
    await store.consume_retry(scope)
    await h.sql(
        "UPDATE report_generation_jobs SET lease_until=now()-interval '1 second' WHERE id=:id",
        {"id": job.id},
    )
    second = await jobs.claim(worker_id="two", organization_id=h.actor.organization_id)
    assert second and second.deadline == job.deadline and second.started_at == job.started_at
    scope = scope.model_copy(update={"fence": second.fence, "worker_id": "two"})
    with pytest.raises(ValueError, match="RETRY_BUDGET"):
        await ReportingUsageStore(sessions).consume_retry(scope)
    await h.sql(
        "UPDATE report_generation_jobs SET lease_until=now()-interval '1 second' WHERE id=:id",
        {"id": job.id},
    )
    third = await jobs.claim(worker_id="three", organization_id=h.actor.organization_id)
    assert third and third.claims == 3
    await h.sql(
        "UPDATE report_generation_jobs SET lease_until=now()-interval '1 second' WHERE id=:id",
        {"id": job.id},
    )
    assert await jobs.claim(worker_id="four", organization_id=h.actor.organization_id) is None
    after = await h.service.get(actor=h.actor, report_id=report.report.id)
    assert after.generation_state == "AI_UNAVAILABLE"
    assert after.selected_version.origin == "METRICS_ONLY"


@pytest.mark.asyncio
async def test_whole_run_token_limits_and_expired_deadline(report_harness: ReportHarness):
    h = report_harness
    report = await h.service.create(
        actor=h.actor,
        command=CreateReportCommand(project_id=h.project_id),
        idempotency_key=str(uuid4()),
    )
    sessions = async_sessionmaker(h.engine, expire_on_commit=False)
    jobs = ReportJobService(GenerationTransactions(sessions, "UTC"))
    job = await jobs.claim(worker_id="one", organization_id=h.actor.organization_id)
    assert job
    scope = ReportingUsageScope(
        organization_id=h.actor.organization_id,
        membership_id=h.actor.membership_id,
        generation_id=job.id,
        fence=job.fence,
        worker_id="one",
    )
    store = ReportingUsageStore(sessions)
    await store.reserve(scope, input_tokens=48000, output_tokens=8000)
    with pytest.raises(ValueError, match="MODEL_BUDGET"):
        await store.reserve(scope, input_tokens=1, output_tokens=1)
    await h.sql(
        "UPDATE report_generation_jobs SET deadline=now()-interval '1 second',"
        "lease_until=now()-interval '1 second' WHERE id=:id",
        {"id": job.id},
    )
    with pytest.raises(ValueError, match="LEASE"):
        await store.reserve_tool(scope, "late")
    assert await jobs.claim(worker_id="restart", organization_id=h.actor.organization_id) is None
    assert (
        await h.service.get(actor=h.actor, report_id=report.report.id)
    ).generation_state == "AI_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted", [False, True])
@pytest.mark.parametrize("before_recorder", [False, True])
async def test_crash_replays_persisted_version(
    report_harness: ReportHarness,
    monkeypatch: pytest.MonkeyPatch,
    exhausted: bool,
    before_recorder: bool,
):
    from uuid import UUID

    from app.core.config import Settings
    from app.modules.identity.domain.auth import AuthenticatedActor
    from app.modules.planning_runs.adapters.ai_runtime import build_model_gateway
    from app.modules.reporting.adapters.narrative_runtime import ReportNarrativeRuntime

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

    sessions = async_sessionmaker(h.engine, expire_on_commit=False)
    transactions = GenerationTransactions(sessions, "UTC")
    runtime = ReportNarrativeRuntime(
        sessions=sessions,
        actors=Actors(),
        gateway=build_model_gateway(Settings(environment="test", ai_provider="mock")),
        timezone="UTC",
    )
    report = await h.service.create(
        actor=h.actor,
        command=CreateReportCommand(project_id=h.project_id),
        idempotency_key=str(uuid4()),
    )
    jobs = ReportJobService(transactions)
    first = await jobs.claim(worker_id="first", organization_id=h.actor.organization_id)
    assert first
    scope = ReportingUsageScope(
        organization_id=h.actor.organization_id,
        membership_id=h.actor.membership_id,
        generation_id=first.id,
        fence=first.fence,
        worker_id="first",
    )
    from app.modules.assistant.adapters.execution_recorder import PostgreSQLTriggerRecorder
    from work_management_ai.runtime.contracts import AgentId, AgentResult

    class ProcessCrash(BaseException):
        pass

    finish = PostgreSQLTriggerRecorder.finish_agent_run

    async def crash_at_proposal(
        recorder: PostgreSQLTriggerRecorder, run_id: UUID, result: AgentResult
    ) -> None:
        if result.agent_id is AgentId.REPORTING:
            raise ProcessCrash()
        await finish(recorder, run_id, result)

    if before_recorder:
        with monkeypatch.context() as patch:
            patch.setattr(PostgreSQLTriggerRecorder, "finish_agent_run", crash_at_proposal)
            with pytest.raises(ProcessCrash):
                await runtime.execute(first, scope)
    else:
        assert await runtime.execute(first, scope)
    proposed = await h.service.get(actor=h.actor, report_id=report.report.id)
    assert proposed.selected_version.origin == "AI_PROPOSED"
    await h.sql(
        "UPDATE report_generation_jobs SET lease_until=now()-interval '1 second' WHERE id=:id",
        {"id": first.id},
    )
    if exhausted:
        await h.sql(
            "UPDATE report_generation_jobs SET claims=3,"
            "deadline=now()-interval '1 second' WHERE id=:id",
            {"id": first.id},
        )
    with pytest.raises(ValueError):
        await ReportingUsageStore(sessions).reserve_tool(scope, "stale")
    recovered = await asyncio.gather(
        *[
            ReportJobService(transactions, runtime).run_once(
                worker_id=worker, organization_id=h.actor.organization_id
            )
            for worker in ("second", "third")
        ]
    )
    assert sum(recovered) == 1
    replay = await h.service.get(actor=h.actor, report_id=report.report.id)
    assert replay.generation_state == "AWAITING_REVIEW"
    assert replay.selected_version == proposed.selected_version
    assert (
        await h.sql(
            "SELECT count(*) FROM report_versions WHERE report_id=:id AND origin='AI_PROPOSED'",
            {"id": report.report.id},
        )
    ).scalar_one() == 1
    assert (
        await h.sql(
            "SELECT attempts FROM report_generation_usage WHERE generation_id=:id", {"id": first.id}
        )
    ).scalar_one() == 2

    assert (
        await h.sql(
            "SELECT count(*) FROM agent_runs WHERE organization_id=:org AND status='RUNNING'",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 0
    run = (
        await h.sql(
            "SELECT status,started_at,completed_at,usage FROM orchestration_runs "
            "WHERE report_id=:id",
            {"id": report.report.id},
        )
    ).one()
    assert (
        run.status == "AWAITING_HUMAN"
        and run.started_at is not None
        and run.completed_at is not None
    )
    assert run.usage["model_attempts"] == 2
