"""Report triggers own durable runs without creating chat facts."""

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.test_report_api_integration import ReportHarness, pytestmark, report_harness

__all__ = ["pytestmark", "report_harness"]


async def inputs(h: ReportHarness) -> tuple[Any, Any]:
    from app.modules.assistant.adapters.agent_runtime import build_agent_registry
    from app.modules.assistant.adapters.trigger_repository import TriggerTransactions
    from app.modules.assistant.application.trigger_service import TriggerService
    from app.modules.assistant.domain.triggers import ReportRequestTrigger
    from app.modules.reporting.domain.commands import CreateReportCommand

    report = await h.service.create(
        actor=h.actor,
        command=CreateReportCommand(project_id=h.project_id, locale="en", narrative_enabled=False),
        idempotency_key=str(uuid4()),
    )
    registry, _ = build_agent_registry()
    service = TriggerService(
        TriggerTransactions(async_sessionmaker(h.engine, expire_on_commit=False)), registry
    )
    trigger = ReportRequestTrigger(
        report_id=report.report.id,
        base_version_id=report.selected_version.id,
        snapshot_hash=report.snapshot.snapshot_hash,
        request_key=str(uuid4()),
    )
    return service, trigger


@pytest.mark.asyncio
async def test_report_trigger_replay_creates_no_chat_message(report_harness: ReportHarness):
    h = report_harness
    service, trigger = await inputs(h)
    runs = await asyncio.gather(*[service.ensure(actor=h.actor, trigger=trigger) for _ in range(2)])
    assert runs[0].id == runs[1].id
    assert runs[0].turn_id is None
    assert runs[0].actor_membership_id == h.actor.membership_id
    for table in ("assistant_messages", "assistant_turns", "assistant_conversations"):
        assert (
            await h.sql(
                f"SELECT count(*) FROM {table} WHERE organization_id=:org",
                {"org": h.actor.organization_id},
            )
        ).scalar_one() == 0
    assert (
        await h.sql(
            "SELECT count(*) FROM orchestration_runs WHERE organization_id=:org",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 1
    for actor in (h.employee, h.foreign):
        with pytest.raises(ValueError):
            await service.ensure(actor=actor, trigger=trigger)
    await h.sql(
        "UPDATE memberships SET role='EMPLOYEE' WHERE id=:id", {"id": h.actor.membership_id}
    )
    with pytest.raises(ValueError):
        await service.ensure(actor=h.actor, trigger=trigger)


@pytest.mark.asyncio
async def test_trigger_shape_and_tenant_fks(report_harness: ReportHarness):
    from app.modules.assistant.domain.triggers import ExecutionTrigger

    h = report_harness
    service, trigger = await inputs(h)
    adapter = TypeAdapter[ExecutionTrigger](ExecutionTrigger)
    for payload in (
        {**trigger.model_dump(), "turn_id": uuid4()},
        {k: v for k, v in trigger.model_dump().items() if k != "base_version_id"},
        {**trigger.model_dump(), "kind": "SUMMARY_JOB"},
    ):
        with pytest.raises(ValidationError):
            adapter.validate_python(payload)
    for patch in (
        {"snapshot_hash": "b" * 64},
        {"report_id": uuid4()},
        {"base_version_id": uuid4()},
    ):
        with pytest.raises(ValueError):
            await service.ensure(actor=h.actor, trigger=trigger.model_copy(update=patch))
    run = await service.ensure(actor=h.actor, trigger=trigger)
    for update in (
        "turn_id=:value",
        "snapshot_hash=:value",
        "base_version_id=:value",
        "actor_membership_id=:value",
        "summary_id=:value",
    ):
        value = "b" * 64 if update.startswith("snapshot_hash") else uuid4()
        with pytest.raises(DBAPIError):
            await h.sql(
                f"UPDATE orchestration_runs SET {update} WHERE id=:id",
                {"value": value, "id": run.id},
            )
    for actor in (h.foreign, h.employee):
        async with h.engine.begin() as connection:
            await connection.execute(text("SET LOCAL ROLE app_runtime"))
            await connection.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true), "
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            assert (
                await connection.execute(text("SELECT count(*) FROM orchestration_runs"))
            ).scalar_one() == 0


@pytest.mark.asyncio
async def test_checkpoint_replay_preserves_usage(report_harness: ReportHarness):
    from app.modules.assistant.adapters.agent_runtime import build_agent_registry
    from app.modules.assistant.adapters.execution_recorder import PostgreSQLTriggerRecorder
    from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory
    from work_management_ai.agents.orchestrator.contracts import (
        ExecutionPlan,
        OrchestratorOutput,
        OrchestratorStatus,
    )
    from work_management_ai.runtime.contracts import AgentBudget
    from work_management_ai.runtime.execution_engine import ExecutionCheckpoint
    from work_management_ai.runtime.triggers import ExecutionScope

    h = report_harness
    service, trigger = await inputs(h)
    run = await service.ensure(actor=h.actor, trigger=trigger)
    registry, _ = build_agent_registry()
    recorder = PostgreSQLTriggerRecorder(
        transaction_factory=PostgreSQLAssistantTransactionFactory(
            async_sessionmaker(h.engine, expire_on_commit=False)
        ),
        registry=registry,
        scope=ExecutionScope(
            organization_id=h.actor.organization_id,
            actor_membership_id=h.actor.membership_id,
            orchestration_run_id=run.id,
            trigger=trigger,
        ),
        actor=h.actor,
    )
    hub = await recorder.ensure_orchestrator_run()
    plan = ExecutionPlan(
        objectives=("report",),
        unavailable_capabilities=("reporting.draft",),
        response_language="en",
    )
    output = OrchestratorOutput(
        execution_plan=plan,
        agent_results=(),
        blocks=(),
        completed_step_ids=(),
        status=OrchestratorStatus.COMPLETED,
        stop_reason="DONE",
        replans_used=0,
        model_refs=(),
    )
    checkpoint = ExecutionCheckpoint(
        orchestration_run_id=run.id,
        sequence=1,
        node="terminal",
        plan=plan,
        completed_step_ids=(),
        agent_result_ids=(),
        remaining_budget=AgentBudget(max_iterations=1, max_tool_calls=0, timeout_seconds=120),
        trigger_result=output,
        usage={"model_attempts": 2},
    )
    await recorder.save_checkpoint(checkpoint)
    await recorder.finish_orchestrator_run(hub, output)
    replay = await service.ensure(actor=h.actor, trigger=trigger)
    assert replay.id == run.id
    assert (await recorder.load_checkpoint(run.id)) == checkpoint
    assert await recorder.ensure_orchestrator_run() == hub
    assert (
        await h.sql(
            "SELECT count(*) FROM agent_runs WHERE organization_id=:org",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 1
    assert (
        await h.sql(
            "SELECT checkpoint->'usage' FROM orchestration_runs WHERE id=:id", {"id": run.id}
        )
    ).scalar_one() == {"model_attempts": 2}
    with pytest.raises(RuntimeError):
        await recorder.load_checkpoint(UUID(int=0))


@pytest.mark.asyncio
async def test_chat_turn_still_owns_one_run(report_harness: ReportHarness):
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import insert, select

    from app.modules.assistant.adapters.database_models import OrchestrationRunModel

    h = report_harness
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        conversation = await client.post(
            "/api/v1/ai/conversations",
            json={"locale": "en", "title": "My work"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert conversation.status_code == 201, conversation.text
        cid = conversation.json()["id"]
        turn = await client.post(
            f"/api/v1/ai/conversations/{cid}/messages",
            json={"message": "Show my work", "locale": "en"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert turn.status_code == 202, turn.text
    async with h.engine.begin() as connection:
        row = dict(
            (
                await connection.execute(
                    select(OrchestrationRunModel.__table__).where(
                        OrchestrationRunModel.organization_id == h.actor.organization_id
                    )
                )
            )
            .mappings()
            .one()
        )
    assert row["trigger_kind"] == "CHAT_TURN"
    assert row["actor_membership_id"] == h.actor.membership_id

    from app.modules.assistant.adapters.agent_runtime import build_agent_registry
    from app.modules.assistant.adapters.trigger_repository import TriggerTransactions
    from app.modules.assistant.application.trigger_service import TriggerService
    from app.modules.assistant.domain.triggers import ChatTurnTrigger

    registry, _ = build_agent_registry()
    service = TriggerService(
        TriggerTransactions(async_sessionmaker(h.engine, expire_on_commit=False)), registry
    )
    original = await service.ensure(actor=h.actor, trigger=ChatTurnTrigger(turn_id=row["turn_id"]))
    assert original.id == row["id"]
    with pytest.raises(ValueError):
        await service.ensure(actor=h.employee, trigger=ChatTurnTrigger(turn_id=row["turn_id"]))
    with pytest.raises(DBAPIError) as duplicate:
        async with h.engine.begin() as connection:
            await connection.execute(insert(OrchestrationRunModel).values(**{**row, "id": uuid4()}))
    assert getattr(duplicate.value.orig, "sqlstate", None) == "23505"


@pytest.mark.asyncio
async def test_summary_trigger_requires_same_project_and_exact_capture(
    report_harness: ReportHarness,
):
    from datetime import UTC, datetime

    from app.modules.assistant.domain.triggers import SummaryJobTrigger, TriggerError
    from app.modules.automations.adapters.digest_repository import DigestTransactions
    from app.modules.automations.adapters.repository import ScheduleTransactions
    from app.modules.automations.application.digest_service import DigestService
    from app.modules.automations.application.schedule_service import ScheduleService
    from app.modules.automations.domain.digests import AuthorizedJobScope, DailySummarySnapshot
    from app.modules.automations.domain.schedules import ScheduleCommand

    h = report_harness
    service, trigger = await inputs(h)
    other_project = uuid4()
    await h.sql(
        "INSERT INTO projects (id,organization_id,name,created_by_membership_id,"
        "updated_by_membership_id) "
        "VALUES (:id,:org,'Another event',:actor,:actor)",
        {"id": other_project, "org": h.actor.organization_id, "actor": h.actor.membership_id},
    )
    sessions = async_sessionmaker(h.engine, expire_on_commit=False)
    schedules = ScheduleService(ScheduleTransactions(sessions))
    digests = DigestService(DigestTransactions(sessions))
    summaries: list[DailySummarySnapshot] = []
    for project in (h.project_id, other_project):
        draft = await schedules.preview(
            h.actor,
            ScheduleCommand(
                project_id=project,
                timezone="UTC",
                weekdays=(1, 2, 3, 4, 5, 6, 7),
                cutoff="00:00",
                send_when_complete=False,
                recipients=(h.actor.membership_id,),
            ),
            0,
            str(uuid4()),
        )
        await schedules.confirm(h.actor, draft.id, 0, str(uuid4()))
        window = (await schedules.get(h.actor, project)).window
        assert window is not None
        summaries.append(
            await digests.trigger(
                AuthorizedJobScope(actor=h.actor, at=datetime.now(UTC)), window.id, "CUTOFF"
            )
        )
    summary_trigger = SummaryJobTrigger(
        report_id=trigger.report_id,
        base_version_id=trigger.base_version_id,
        snapshot_hash=trigger.snapshot_hash,
        summary_id=summaries[0].id,
        request_key=str(uuid4()),
    )
    run = await service.ensure(actor=h.actor, trigger=summary_trigger)
    assert run.summary_id == summaries[0].id
    with pytest.raises(TriggerError, match="SUMMARY_MISMATCH"):
        await service.ensure(
            actor=h.actor,
            trigger=summary_trigger.model_copy(update={"summary_id": summaries[1].id}),
        )
    with pytest.raises(DBAPIError) as mismatch:
        await h.sql(
            "UPDATE orchestration_runs SET summary_id=:summary WHERE id=:id",
            {"summary": summaries[1].id, "id": run.id},
        )
    assert getattr(mismatch.value.orig, "sqlstate", None) == "23503"
