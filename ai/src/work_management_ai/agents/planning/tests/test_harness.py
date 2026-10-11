from typing import Literal
from uuid import uuid4

import pytest
from pydantic import BaseModel

from work_management_ai.agents.orchestrator.contracts import ActorContextResolverPort
from work_management_ai.agents.planning.contracts import (
    PlanningAgentOutput,
    PlanningOperation,
)
from work_management_ai.agents.planning.harness import PlanningAgentHarness
from work_management_ai.model_gateway.contracts import (
    StructuredModelRequest,
    StructuredModelResponse,
)
from work_management_ai.model_gateway.mock import MockModelGateway
from work_management_ai.runtime.contracts import (
    ActorReference,
    AgentBudget,
    AgentHandoff,
    AgentId,
    AgentRunStatus,
    ProposedAction,
    ResolvedActorContext,
    RiskLevel,
    ToolExecutionRequest,
    ToolExecutionResult,
    ToolExecutorPort,
)
from work_management_ai.runtime.manifests import AgentManifest, load_yaml_resource
from work_management_ai.tools.planning.manage_run.adapter import PlanningRunToolAdapter
from work_management_ai.tools.planning.manage_run.contracts import (
    PlanningRunApplicationPort,
    PlanningRunToolInput,
)


class StaticActorResolver(ActorContextResolverPort):
    def __init__(self, actor: ResolvedActorContext) -> None:
        self.actor = actor

    async def resolve(self, reference: ActorReference) -> ResolvedActorContext:
        assert reference.membership_id == self.actor.membership_id
        assert reference.organization_id == self.actor.organization_id
        return self.actor


class RecordingToolExecutor:
    def __init__(self, result: ToolExecutionResult) -> None:
        self.result = result
        self.requests: list[ToolExecutionRequest] = []

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        self.requests.append(request)
        return self.result


class FakePlanningApplication(PlanningRunApplicationPort):
    def __init__(self, output: PlanningAgentOutput) -> None:
        self.output = output
        self.calls: list[tuple[ActorReference, PlanningRunToolInput, str]] = []

    async def manage_run(
        self,
        *,
        actor: ActorReference,
        value: PlanningRunToolInput,
        idempotency_key: str,
    ) -> PlanningAgentOutput:
        self.calls.append((actor, value, idempotency_key))
        return self.output


def _actor(role: Literal["ADMIN", "MANAGER", "EMPLOYEE"] = "MANAGER") -> ResolvedActorContext:
    return ResolvedActorContext(
        membership_id=uuid4(),
        organization_id=uuid4(),
        role=role,
        is_active=True,
    )


def _handoff(
    actor: ResolvedActorContext,
    *,
    operation: PlanningOperation,
    workflow_run_id: str | None = None,
    proposal_id: str | None = None,
    expected_proposal_version: int | None = None,
    manager_instruction: str | None = None,
) -> AgentHandoff:
    capability = {
        PlanningOperation.CREATE: "planning.create",
        PlanningOperation.RESUME_INPUT: "planning.resume",
        PlanningOperation.REVISE: "planning.revise",
        PlanningOperation.EXPLAIN: "planning.explain",
    }[operation]
    return AgentHandoff(
        orchestration_run_id=uuid4(),
        parent_agent_run_id=uuid4(),
        target_agent_id=AgentId.PLANNING,
        target_agent_version="1.0.0",
        capability=capability,
        objective="Create or revise a planning proposal",
        typed_input={
            "operation": operation.value,
            "locale": "en",
            "brief": "Plan Project A",
            "workflow_run_id": workflow_run_id,
            "proposal_id": proposal_id,
            "expected_proposal_version": expected_proposal_version,
            "manager_instruction": manager_instruction,
        },
        context_references=(),
        actor=ActorReference(
            membership_id=actor.membership_id,
            organization_id=actor.organization_id,
        ),
        budget=AgentBudget(
            max_iterations=8,
            max_tool_calls=12,
            max_handoffs=0,
            max_replans=1,
            timeout_seconds=120,
        ),
        step_id="planning",
        idempotency_key=f"planning-test:{uuid4()}",
    )


def _output(operation: PlanningOperation) -> PlanningAgentOutput:
    return PlanningAgentOutput(
        operation=operation,
        workflow_run_id=uuid4(),
        workflow_status="WAITING_FOR_DECISION",
        proposal_id=uuid4(),
        proposal_version=1,
        approval_id=uuid4(),
        awaiting="MANAGER_DECISION",
        public_summary="The proposal is ready for Manager review.",
    )


def _tool_result(output: PlanningAgentOutput) -> ToolExecutionResult:
    return ToolExecutionResult(
        status="SUCCEEDED",
        typed_output=output.model_dump(mode="json"),
        evidence=(),
    )


def _step_plan(operation: PlanningOperation) -> dict[str, object]:
    skill = (
        "revise_project_plan@1"
        if operation is PlanningOperation.REVISE
        else "create_project_plan@1"
    )
    return {
        "skill_reference": skill,
        "tool_id": "planning.manage_run",
        "tool_input": {},
        "requested_handoff": None,
    }


def _harness(
    *,
    actor: ResolvedActorContext,
    fixtures: dict[str, object],
    tool_executor: ToolExecutorPort,
) -> PlanningAgentHarness:
    return PlanningAgentHarness(
        model_gateway=MockModelGateway(fixtures=fixtures),
        tool_executor=tool_executor,
        actor_resolver=StaticActorResolver(actor),
    )


def test_planning_manifest_is_manager_admin_proposal_only() -> None:
    manifest = load_yaml_resource("work_management_ai.agents.planning", "agent.yaml", AgentManifest)

    assert manifest.agent.id is AgentId.PLANNING
    assert manifest.permissions.roles == ("ADMIN", "MANAGER")
    assert manifest.permissions.risk_ceiling is RiskLevel.PROPOSAL_ONLY
    assert manifest.approval.produced_writes == "ALWAYS"
    assert manifest.allowed_tools == ("planning.manage_run@1",)


@pytest.mark.asyncio
async def test_create_operation_calls_manage_run_once() -> None:
    actor = _actor()
    expected = _output(PlanningOperation.CREATE)
    application = FakePlanningApplication(expected)
    harness = _harness(
        actor=actor,
        fixtures={"planning_agent.en.step_plan": _step_plan(PlanningOperation.CREATE)},
        tool_executor=PlanningRunToolAdapter(application=application),
    )

    result = await harness.run(_handoff(actor, operation=PlanningOperation.CREATE))

    assert result.status is AgentRunStatus.AWAITING_HUMAN
    assert PlanningAgentOutput.model_validate(result.typed_output) == expected
    assert len(application.calls) == 1
    assert application.calls[0][1].operation is PlanningOperation.CREATE


@pytest.mark.asyncio
async def test_resume_requires_existing_await_manager_input_reference() -> None:
    actor = _actor()
    executor = RecordingToolExecutor(_tool_result(_output(PlanningOperation.RESUME_INPUT)))
    harness = _harness(
        actor=actor,
        fixtures={"planning_agent.en.step_plan": _step_plan(PlanningOperation.RESUME_INPUT)},
        tool_executor=executor,
    )

    result = await harness.run(_handoff(actor, operation=PlanningOperation.RESUME_INPUT))

    assert result.status is AgentRunStatus.FAILED
    assert result.stop_reason == "PLANNING_INPUT_INVALID"
    assert executor.requests == []


@pytest.mark.asyncio
async def test_revision_requires_proposal_and_exact_base_version() -> None:
    actor = _actor()
    executor = RecordingToolExecutor(_tool_result(_output(PlanningOperation.REVISE)))
    harness = _harness(
        actor=actor,
        fixtures={"planning_agent.en.step_plan": _step_plan(PlanningOperation.REVISE)},
        tool_executor=executor,
    )

    result = await harness.run(
        _handoff(
            actor,
            operation=PlanningOperation.REVISE,
            workflow_run_id=str(uuid4()),
            manager_instruction="Move the final milestone",
        )
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.stop_reason == "PLANNING_INPUT_INVALID"
    assert executor.requests == []


@pytest.mark.asyncio
async def test_revision_model_output_is_deterministically_verified() -> None:
    actor = _actor()
    executor = RecordingToolExecutor(
        ToolExecutionResult(
            status="REJECTED",
            typed_output={},
            safe_error_code="PLANNING_REVISION_INVALID",
        )
    )
    harness = _harness(
        actor=actor,
        fixtures={"planning_agent.en.step_plan": _step_plan(PlanningOperation.REVISE)},
        tool_executor=executor,
    )

    result = await harness.run(
        _handoff(
            actor,
            operation=PlanningOperation.REVISE,
            workflow_run_id=str(uuid4()),
            proposal_id=str(uuid4()),
            expected_proposal_version=2,
            manager_instruction="Create a dependency cycle",
        )
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.safe_error_code == "PLANNING_REVISION_INVALID"


@pytest.mark.asyncio
async def test_revision_does_not_persist_or_approve_inside_ai_package() -> None:
    actor = _actor()
    expected = _output(PlanningOperation.REVISE)
    executor = RecordingToolExecutor(_tool_result(expected))
    harness = _harness(
        actor=actor,
        fixtures={},
        tool_executor=executor,
    )

    result = await harness.run(
        _handoff(
            actor,
            operation=PlanningOperation.REVISE,
            workflow_run_id=str(expected.workflow_run_id),
            proposal_id=str(expected.proposal_id),
            expected_proposal_version=1,
            manager_instruction="Move milestone by one week",
        )
    )

    assert result.status is AgentRunStatus.AWAITING_HUMAN
    assert result.iterations_used == 0
    assert len(executor.requests) == 1
    assert not {"approved", "decision", "created_business_ids"}.intersection(
        executor.requests[0].typed_input
    )
    assert result.proposed_actions == (
        ProposedAction(
            action_type="planning.proposal_review",
            risk=RiskLevel.PROPOSAL_ONLY,
            requires_human_gate=True,
            reference_id=expected.proposal_id,
        ),
    )


@pytest.mark.asyncio
async def test_agent_cannot_call_approval_or_work_mutation_tool() -> None:
    actor = _actor()
    executor = RecordingToolExecutor(_tool_result(_output(PlanningOperation.CREATE)))
    harness = _harness(
        actor=actor,
        fixtures={
            "planning_agent.en.step_plan": {
                "skill_reference": "create_project_plan@1",
                "tool_id": "approval.decide",
                "tool_input": {"decision": "APPROVE"},
                "requested_handoff": None,
            }
        },
        tool_executor=executor,
    )

    result = await harness.run(_handoff(actor, operation=PlanningOperation.CREATE))

    assert result.status is AgentRunStatus.FAILED
    assert result.safe_error_code == "PLANNING_MANUAL_EDITABLE_FALLBACK"
    assert executor.requests == []


@pytest.mark.asyncio
async def test_employee_handoff_is_rejected_before_model_or_tool() -> None:
    actor = _actor(role="EMPLOYEE")
    executor = RecordingToolExecutor(_tool_result(_output(PlanningOperation.CREATE)))
    harness = _harness(
        actor=actor,
        fixtures={"planning_agent.en.step_plan": _step_plan(PlanningOperation.CREATE)},
        tool_executor=executor,
    )

    result = await harness.run(_handoff(actor, operation=PlanningOperation.CREATE))

    assert result.status is AgentRunStatus.FAILED
    assert result.stop_reason == "PLANNING_ROLE_FORBIDDEN"
    assert result.iterations_used == 0
    assert executor.requests == []


@pytest.mark.asyncio
async def test_provider_timeout_returns_manual_editable_fallback() -> None:
    actor = _actor()
    executor = RecordingToolExecutor(_tool_result(_output(PlanningOperation.CREATE)))
    harness = _harness(
        actor=actor,
        fixtures={"planning_agent.en.step_plan": TimeoutError("private provider detail")},
        tool_executor=executor,
    )

    result = await harness.run(_handoff(actor, operation=PlanningOperation.CREATE))

    assert result.status is AgentRunStatus.FAILED
    assert result.safe_error_code == "PLANNING_MANUAL_EDITABLE_FALLBACK"
    assert "private provider detail" not in str(result.typed_output)
    assert executor.requests == []


@pytest.mark.asyncio
async def test_planning_request_for_assignment_returns_requested_handoff() -> None:
    actor = _actor()
    executor = RecordingToolExecutor(_tool_result(_output(PlanningOperation.CREATE)))
    harness = _harness(
        actor=actor,
        fixtures={
            "planning_agent.en.step_plan": {
                "skill_reference": "create_project_plan@1",
                "tool_id": None,
                "tool_input": {},
                "requested_handoff": {
                    "target_capability": "assignment.recommend",
                    "objective": "Recommend an assignee",
                    "typed_input": {"task_reference": "t1"},
                },
            }
        },
        tool_executor=executor,
    )

    result = await harness.run(_handoff(actor, operation=PlanningOperation.CREATE))

    assert result.requested_handoff is not None
    assert result.requested_handoff.target_capability == "assignment.recommend"
    assert executor.requests == []


@pytest.mark.asyncio
async def test_progressive_tool_selection_is_deterministic_and_spends_no_model_attempts() -> None:
    actor = _actor()
    handoff = _handoff(actor, operation=PlanningOperation.CREATE)
    handoff = handoff.model_copy(
        update={"typed_input": {**handoff.typed_input, "progressive": True}}
    )
    output = _output(PlanningOperation.CREATE).model_copy(
        update={
            "workflow_status": "QUEUED",
            "proposal_id": None,
            "proposal_version": None,
            "approval_id": None,
            "awaiting": "NONE",
        }
    )

    class ForbiddenGateway:
        async def generate_structured[T: BaseModel](
            self, request: StructuredModelRequest[T]
        ) -> StructuredModelResponse[T]:
            raise AssertionError("progressive tool selection must not call a model")

    tools = RecordingToolExecutor(_tool_result(output))
    harness = PlanningAgentHarness(
        model_gateway=ForbiddenGateway(),
        tool_executor=tools,
        actor_resolver=StaticActorResolver(actor),
    )
    result = await harness.run(handoff)
    assert result.status is AgentRunStatus.COMPLETED
    assert len(tools.requests) == 1
