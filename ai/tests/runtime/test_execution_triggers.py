"""Trusted report entrypoints are separate from conversation turns."""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter, ValidationError


def test_trigger_contract_is_exclusive_and_authority_free():
    from work_management_ai.runtime.triggers import ExecutionTrigger, ReportRequestTrigger

    trigger = ReportRequestTrigger(
        report_id=uuid4(),
        base_version_id=uuid4(),
        snapshot_hash="a" * 64,
        request_key="safe-request-key",
    )
    adapter = TypeAdapter[ExecutionTrigger](ExecutionTrigger)
    assert adapter.validate_json(trigger.model_dump_json()) == trigger
    for patch in (
        {"turn_id": uuid4()},
        {"role": "ADMIN"},
        {"kind": "UNKNOWN"},
        {"snapshot_hash": "invalid"},
        {"kind": "SUMMARY_JOB"},
    ):
        with pytest.raises(ValidationError):
            adapter.validate_python({**trigger.model_dump(), **patch})


@pytest.mark.asyncio
async def test_non_chat_checkpoint_replays_without_chat_or_resetting_usage():
    from work_management_ai.agents.orchestrator.contracts import (
        ExecutionPlan,
        OrchestratorOutput,
        OrchestratorStatus,
        OrchestratorTriggerInput,
    )
    from work_management_ai.runtime.contracts import (
        ActorReference,
        AgentBudget,
        AgentHandoff,
        AgentResult,
        ResponseBlock,
    )
    from work_management_ai.runtime.execution_engine import (
        AgentExecutionEngine,
        ExecutionCheckpoint,
        RecordedAgentRun,
    )
    from work_management_ai.runtime.triggers import ReportRequestTrigger

    run_id = uuid4()
    output = OrchestratorOutput(
        execution_plan=ExecutionPlan(
            objectives=("report",),
            unavailable_capabilities=("reporting.draft",),
            response_language="en",
        ),
        agent_results=(),
        blocks=(),
        completed_step_ids=(),
        status=OrchestratorStatus.COMPLETED,
        stop_reason="DONE",
        replans_used=0,
        model_refs=(),
    )
    assert output.execution_plan is not None
    checkpoint = ExecutionCheckpoint(
        orchestration_run_id=run_id,
        sequence=1,
        node="terminal",
        plan=output.execution_plan,
        completed_step_ids=(),
        agent_result_ids=(),
        remaining_budget=AgentBudget(max_iterations=1, max_tool_calls=0, timeout_seconds=120),
        trigger_result=output,
        usage={"model_attempts": 2},
    )

    class Recorder:
        async def load_checkpoint(self, orchestration_run_id: UUID) -> ExecutionCheckpoint:
            return checkpoint

        async def save_checkpoint(self, checkpoint: ExecutionCheckpoint) -> None:
            raise AssertionError("replay cannot reset checkpoint usage")

        async def start_agent_run(self, handoff: AgentHandoff) -> RecordedAgentRun:
            raise AssertionError("replay cannot create an Agent Run")

        async def finish_agent_run(self, run_id: UUID, result: AgentResult) -> None:
            raise AssertionError("replay cannot change an Agent Run")

        async def append_public_blocks(
            self,
            conversation_id: UUID,
            turn_id: UUID,
            blocks: tuple[ResponseBlock, ...],
            dedupe_key: str,
        ) -> None:
            raise AssertionError("non-chat must never write a transcript")

    class Hub:
        async def run_trigger(self, value: OrchestratorTriggerInput) -> OrchestratorOutput:
            raise AssertionError("terminal checkpoint must replay before invoking the hub")

    value = OrchestratorTriggerInput(
        orchestration_run_id=run_id,
        locale="en",
        actor=ActorReference(organization_id=uuid4(), membership_id=uuid4()),
        trigger=ReportRequestTrigger(
            report_id=uuid4(),
            base_version_id=uuid4(),
            snapshot_hash="a" * 64,
            request_key="safe-request-key",
        ),
    )
    from work_management_ai.runtime.execution_engine import trigger_fingerprint

    checkpoint = checkpoint.model_copy(update={"trigger_fingerprint": trigger_fingerprint(value)})
    engine = AgentExecutionEngine(Hub())
    assert await engine.execute_trigger(value=value, recorder=Recorder()) == output
    wrong = value.model_copy(
        update={"trigger": value.trigger.model_copy(update={"report_id": uuid4()})}
    )
    with pytest.raises(RuntimeError, match="CHECKPOINT_TRIGGER_MISMATCH"):
        await engine.execute_trigger(value=wrong, recorder=Recorder())


@pytest.mark.asyncio
async def test_only_hub_dispatches_specialist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from work_management_ai.agents.orchestrator.contracts import OrchestratorTriggerInput
    from work_management_ai.agents.orchestrator.harness import OrchestratorHarness
    from work_management_ai.agents.orchestrator.tests.test_harness import (
        RecordingSpecialistRunner,
        StaticActorResolver,
        _resolved_actor,  # pyright: ignore[reportPrivateUsage]
        _write_specialist_manifest,  # pyright: ignore[reportPrivateUsage]
    )
    from work_management_ai.model_gateway.mock import MockModelGateway
    from work_management_ai.runtime.agent_registry import AgentRegistry
    from work_management_ai.runtime.contracts import ActorReference, AgentId
    from work_management_ai.runtime.policy_guard import PolicyGuard
    from work_management_ai.runtime.skill_registry import SkillRegistry
    from work_management_ai.runtime.tool_registry import ToolRegistry
    from work_management_ai.runtime.triggers import ReportRequestTrigger

    registry = AgentRegistry(
        skill_registry=SkillRegistry(),
        tool_registry=ToolRegistry(),
        evaluator_ids=frozenset({"reporting_evaluator@1"}),
    )
    package = _write_specialist_manifest(
        tmp_path,
        monkeypatch,
        package_name="test_reporting_boundary",
        agent_id=AgentId.REPORTING,
        capability="reporting.draft",
        roles=("MANAGER", "ADMIN"),
        risk="PROPOSAL_ONLY",
    )
    registry.register_resource(*package)
    actor = _resolved_actor()
    runner = RecordingSpecialistRunner()
    hub = OrchestratorHarness(
        model_gateway=MockModelGateway(fixtures={}),
        registry=registry,
        policy_guard=PolicyGuard(),
        actor_resolver=StaticActorResolver(actor),
        specialists=runner,
    )
    value = OrchestratorTriggerInput(
        orchestration_run_id=uuid4(),
        locale="vi",
        actor=ActorReference(
            organization_id=actor.organization_id, membership_id=actor.membership_id
        ),
        trigger=ReportRequestTrigger(
            report_id=uuid4(),
            base_version_id=uuid4(),
            snapshot_hash="a" * 64,
            request_key="safe-request-key",
        ),
    )
    output = await hub.run_trigger(value)
    assert output.completed_step_ids == ("reporting",)
    assert len(runner.handoffs) == 1
    handoff = runner.handoffs[0]
    assert handoff.target_agent_id == AgentId.REPORTING
    assert handoff.orchestration_run_id == value.orchestration_run_id
    assert handoff.typed_input["report_id"] == str(value.trigger.report_id)
    assert handoff.idempotency_key == f"{value.orchestration_run_id}:reporting"
    with pytest.raises(ValueError):
        PolicyGuard().authorize_handoff(
            current_actor=actor,
            parent_agent_id=AgentId.PLANNING,
            handoff=handoff,
            manifest=registry.resolve(AgentId.REPORTING, "1.0.0", 5).manifest,
        )
    employee = _resolved_actor("EMPLOYEE")
    denied_hub = OrchestratorHarness(
        model_gateway=MockModelGateway(fixtures={}),
        registry=registry,
        policy_guard=PolicyGuard(),
        actor_resolver=StaticActorResolver(employee),
        specialists=runner,
    )
    denied = await denied_hub.run_trigger(
        value.model_copy(
            update={
                "actor": ActorReference(
                    organization_id=employee.organization_id, membership_id=employee.membership_id
                )
            }
        )
    )
    assert denied.status.value == "FAILED"
    assert len(runner.handoffs) == 1


def test_trigger_graph_has_no_transcript_or_intent_inference_nodes():
    from work_management_ai.agents.orchestrator.workflows.graph import TRIGGER_NODES

    assert TRIGGER_NODES == ("resolve_and_dispatch_trigger", "typed_result")
    assert not {"intake_turn", "plan_objective", "synthesize"}.intersection(TRIGGER_NODES)
