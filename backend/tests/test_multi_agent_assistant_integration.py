"""Bounded integration acceptance for the three activated Phase 2 Agents."""

# pyright: reportUnknownParameterType=false, reportMissingParameterType=false

import json
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

import app.modules.assistant.adapters.agent_runtime as agent_runtime_module
from app.core.config import Settings
from app.modules.assistant.adapters.agent_runtime import AssistantTurnExecutor, build_agent_registry
from app.modules.assistant.application.ports import AssistantConversationSnapshot
from app.modules.assistant.domain.models import (
    AssistantConversation,
    AssistantJob,
    AssistantMessage,
    AssistantTurn,
    MessageRole,
    OrchestrationRun,
)
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.planning_runs.adapters.ai_runtime import build_model_gateway
from work_management_ai.agents.orchestrator.contracts import (
    ActiveConversationContext,
    ExecutionPlan,
    OrchestratorInput,
    OrchestratorOutput,
    OrchestratorStatus,
)
from work_management_ai.agents.orchestrator.harness import OrchestratorHarness
from work_management_ai.agents.planning.harness import PlanningAgentHarness
from work_management_ai.agents.work_intelligence.contracts import WorkStepPlan
from work_management_ai.model_gateway.contracts import ModelMessage, StructuredModelRequest
from work_management_ai.model_gateway.errors import ModelTimeoutError
from work_management_ai.model_gateway.mock import MockModelGateway
from work_management_ai.runtime.contracts import (
    ActorReference,
    AgentBudget,
    AgentHandoff,
    AgentId,
    AgentResult,
    AgentRunStatus,
    RequestedHandoff,
    ResolvedActorContext,
    ResponseBlock,
    RiskLevel,
    ToolExecutionResult,
)
from work_management_ai.runtime.execution_engine import (
    DurableSpecialistRunner,
    RecordedAgentRun,
)
from work_management_ai.runtime.policy_guard import PolicyGuard


class _ActorResolver:
    def __init__(self, *, role: Literal["ADMIN", "MANAGER", "EMPLOYEE"] = "MANAGER") -> None:
        self.actor = ResolvedActorContext(
            membership_id=uuid4(),
            organization_id=uuid4(),
            role=role,
            is_active=True,
        )

    async def resolve(self, reference: ActorReference) -> ResolvedActorContext:
        assert reference.membership_id == self.actor.membership_id
        assert reference.organization_id == self.actor.organization_id
        return self.actor


class _RecordingSpecialists:
    def __init__(self, results: list[AgentResult] | None = None) -> None:
        self.handoffs: list[AgentHandoff] = []
        self.results = list(results or [])

    async def run_specialist(self, handoff: AgentHandoff) -> AgentResult:
        self.handoffs.append(handoff)
        if self.results:
            return self.results.pop(0)
        return AgentResult(
            agent_id=handoff.target_agent_id,
            agent_version=handoff.target_agent_version,
            status=AgentRunStatus.COMPLETED,
            typed_output={"summary": "verified"},
            stop_reason="COMPLETED",
        )


def _value(resolver: _ActorResolver, message: str, *, locale: str = "en") -> OrchestratorInput:
    return OrchestratorInput(
        orchestration_run_id=uuid4(),
        conversation_id=uuid4(),
        turn_id=uuid4(),
        message=message,
        locale=locale,  # type: ignore[arg-type]
        actor=ActorReference(
            membership_id=resolver.actor.membership_id,
            organization_id=resolver.actor.organization_id,
        ),
        active_context=ActiveConversationContext(recent_messages=()),
    )


def _orchestrator(
    *, resolver: _ActorResolver, specialists: _RecordingSpecialists, fixtures: dict[str, object]
) -> OrchestratorHarness:
    registry, _ = build_agent_registry()
    return OrchestratorHarness(
        model_gateway=MockModelGateway(fixtures=fixtures),
        registry=registry,
        policy_guard=PolicyGuard(),
        actor_resolver=resolver,
        specialists=specialists,
    )


def _multi_intent_plan() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "objectives": ["Read permitted work and prepare a proposal"],
        "steps": [
            {
                "step_id": "read_work",
                "target_agent_id": "work_intelligence",
                "target_agent_version": "1.0.0",
                "capability": "work.read_project",
                "objective": "Read permitted Project facts",
                "typed_input": {"reference": "Project A"},
                "depends_on": [],
                "mode": "READ_ONLY",
            },
            {
                "step_id": "draft_plan",
                "target_agent_id": "planning",
                "target_agent_version": "1.0.0",
                "capability": "planning.create",
                "objective": "Draft a Project proposal",
                "typed_input": {"brief": "Plan Project A"},
                "depends_on": ["read_work"],
                "mode": "PROPOSAL",
            },
        ],
        "unavailable_capabilities": [],
        "response_language": "en",
    }


@pytest.mark.asyncio
async def test_multi_intent_turn_records_orchestrator_work_and_planning_agent_runs() -> None:
    resolver = _ActorResolver()
    specialists = _RecordingSpecialists()
    value = _value(resolver, "Read Project A, then prepare a plan")
    output = await _orchestrator(
        resolver=resolver,
        specialists=specialists,
        fixtures={
            "orchestrator.en.plan": _multi_intent_plan(),
            "orchestrator.en.synthesize": {
                "blocks": [{"kind": "text", "text": "The verified proposal is ready."}]
            },
        },
    ).run_turn(value)

    recorded_agents = [
        AgentId.ORCHESTRATOR,
        *[item.target_agent_id for item in specialists.handoffs],
    ]
    assert recorded_agents == [
        AgentId.ORCHESTRATOR,
        AgentId.WORK_INTELLIGENCE,
        AgentId.PLANNING,
    ]
    assert output.completed_step_ids == ("read_work", "draft_plan")
    assert all(
        item.orchestration_run_id == value.orchestration_run_id for item in specialists.handoffs
    )


@pytest.mark.asyncio
async def test_requested_handoff_has_orchestrator_as_next_parent() -> None:
    resolver = _ActorResolver()
    requested = AgentResult(
        agent_id=AgentId.WORK_INTELLIGENCE,
        agent_version="1.0.0",
        status=AgentRunStatus.COMPLETED,
        typed_output={"summary": "planning is required"},
        requested_handoff=RequestedHandoff(
            target_capability="planning.create",
            objective="Create a plan from permitted evidence",
            typed_input={"brief": "Plan Project A"},
        ),
        stop_reason="REQUESTED_HANDOFF",
    )
    specialists = _RecordingSpecialists([requested])
    initial = _multi_intent_plan()
    initial["steps"] = [initial["steps"][0]]  # type: ignore[index]
    value = _value(resolver, "Read and plan Project A")
    output = await _orchestrator(
        resolver=resolver,
        specialists=specialists,
        fixtures={
            "orchestrator.en.plan": initial,
            "orchestrator.en.replan.1": _multi_intent_plan(),
            "orchestrator.en.synthesize": {
                "blocks": [{"kind": "text", "text": "The proposal is ready."}]
            },
        },
    ).run_turn(value)

    expected_parent = uuid5(NAMESPACE_URL, f"orchestrator:{value.turn_id}")
    assert output.replans_used == 1
    assert [handoff.parent_agent_run_id for handoff in specialists.handoffs] == [
        expected_parent,
        expected_parent,
    ]


@pytest.mark.asyncio
async def test_crash_resume_duplicates_zero_messages_tools_proposals_approvals_or_rows() -> None:
    result = AgentResult(
        agent_id=AgentId.WORK_INTELLIGENCE,
        agent_version="1.0.0",
        status=AgentRunStatus.COMPLETED,
        typed_output={"summary": "persisted"},
        stop_reason="COMPLETED",
    )
    handoff = AgentHandoff(
        orchestration_run_id=uuid4(),
        parent_agent_run_id=uuid4(),
        target_agent_id=AgentId.WORK_INTELLIGENCE,
        target_agent_version="1.0.0",
        capability="work.answer_question",
        objective="Answer from permitted evidence",
        typed_input={"locale": "en", "question": "What is next?"},
        context_references=(),
        actor=ActorReference(membership_id=uuid4(), organization_id=uuid4()),
        budget=AgentBudget(max_iterations=2, max_tool_calls=1, timeout_seconds=10),
        step_id="read_work",
        idempotency_key="turn:read_work",
    )
    side_effects = {name: 0 for name in ("messages", "tools", "proposals", "approvals", "rows")}

    class Recorder:
        async def start_agent_run(self, value: AgentHandoff) -> RecordedAgentRun:
            assert value.idempotency_key == handoff.idempotency_key
            return RecordedAgentRun(
                id=uuid4(), status=AgentRunStatus.COMPLETED, replayed_result=result
            )

        async def finish_agent_run(self, *_):
            side_effects["rows"] += 1

    class Harness:
        async def run(self, handoff: AgentHandoff) -> AgentResult:
            assert handoff.target_agent_id is AgentId.WORK_INTELLIGENCE
            side_effects["tools"] += 1
            raise AssertionError("a completed Agent Run must replay")

    replayed = await DurableSpecialistRunner(
        recorder=Recorder(),  # type: ignore[arg-type]
        harnesses={AgentId.WORK_INTELLIGENCE: Harness()},
    ).run_specialist(handoff)

    assert replayed == result
    assert side_effects == {"messages": 0, "tools": 0, "proposals": 0, "approvals": 0, "rows": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["What work is next?", "Create a Project plan"])
async def test_provider_timeout_preserves_manual_work_and_planning_paths(message: str) -> None:
    resolver = _ActorResolver()
    specialists = _RecordingSpecialists()
    output = await _orchestrator(
        resolver=resolver,
        specialists=specialists,
        fixtures={
            "orchestrator.en.plan": ModelTimeoutError("private provider detail"),
            "orchestrator.en.repair": ModelTimeoutError("private provider detail"),
        },
    ).run_turn(_value(resolver, message))

    assert output.status.value == "FAILED"
    assert output.blocks[0].model_dump(mode="json") == {
        "kind": "safe_error",
        "code": "ORCHESTRATOR_MANUAL_FALLBACK",
        "message_key": "ai.error.manualFallback",
    }
    assert specialists.handoffs == []


@pytest.mark.asyncio
async def test_task8_approval_is_the_only_path_to_business_graph_creation() -> None:
    resolver = _ActorResolver()
    workflow_run_id, proposal_id, approval_id = uuid4(), uuid4(), uuid4()

    class ProposalOnlyTool:
        async def execute(self, _):
            return ToolExecutionResult(
                status="SUCCEEDED",
                typed_output={
                    "operation": "CREATE",
                    "workflow_run_id": str(workflow_run_id),
                    "workflow_status": "WAITING_FOR_DECISION",
                    "proposal_id": str(proposal_id),
                    "proposal_version": 1,
                    "approval_id": str(approval_id),
                    "awaiting": "MANAGER_DECISION",
                    "public_summary": "Review the proposal before applying it.",
                },
            )

    harness = PlanningAgentHarness(
        model_gateway=MockModelGateway(
            fixtures={
                "planning_agent.en.step_plan": {
                    "skill_reference": "create_project_plan@1",
                    "tool_id": "planning.manage_run",
                    "tool_input": {},
                    "requested_handoff": None,
                }
            }
        ),
        tool_executor=ProposalOnlyTool(),  # type: ignore[arg-type]
        actor_resolver=resolver,
    )
    result = await harness.run(
        AgentHandoff(
            orchestration_run_id=uuid4(),
            parent_agent_run_id=uuid4(),
            target_agent_id=AgentId.PLANNING,
            target_agent_version="1.0.0",
            capability="planning.create",
            objective="Create a Project proposal",
            typed_input={"operation": "CREATE", "locale": "en", "brief": "Plan a launch"},
            context_references=(),
            actor=ActorReference(
                membership_id=resolver.actor.membership_id,
                organization_id=resolver.actor.organization_id,
            ),
            budget=AgentBudget(
                max_iterations=8,
                max_tool_calls=12,
                max_replans=1,
                timeout_seconds=120,
            ),
            step_id="draft_plan",
            idempotency_key="turn:draft_plan",
        )
    )

    assert result.status is AgentRunStatus.AWAITING_HUMAN
    assert result.proposed_actions[0].risk is RiskLevel.PROPOSAL_ONLY
    assert result.proposed_actions[0].requires_human_gate
    assert result.proposed_actions[0].reference_id == proposal_id


@pytest.mark.asyncio
async def test_mock_provider_supplies_bounded_assistant_agent_fixtures_without_credentials() -> (
    None
):
    gateway = build_model_gateway(Settings(environment="test", ai_provider="mock"))
    request = StructuredModelRequest(
        invocation_key="orchestrator.en.plan",
        messages=(
            ModelMessage(role="system", content="bounded test"),
            ModelMessage(
                role="user",
                content=json.dumps(
                    {
                        "locale": "en",
                        "message": "Create a Project plan",
                        "specialist_catalog": [
                            {
                                "agent_id": "planning",
                                "agent_version": "1.0.0",
                                "capabilities": ["planning.create"],
                            }
                        ],
                    }
                ),
            ),
        ),
        output_schema=ExecutionPlan,
        timeout_seconds=5,
    )

    planning_plan = (await gateway.generate_structured(request)).parsed
    work_plan = (
        await gateway.generate_structured(
            StructuredModelRequest(
                invocation_key="orchestrator.vi.plan",
                messages=(
                    ModelMessage(role="system", content="bounded test"),
                    ModelMessage(
                        role="user",
                        content=json.dumps(
                            {
                                "locale": "vi",
                                "message": "Task hiện tại của tôi là gì?",
                                "specialist_catalog": [
                                    {
                                        "agent_id": "work_intelligence",
                                        "agent_version": "1.0.0",
                                        "capabilities": ["work.read_my_tasks"],
                                    }
                                ],
                            },
                            ensure_ascii=False,
                        ),
                    ),
                ),
                output_schema=ExecutionPlan,
                timeout_seconds=5,
            )
        )
    ).parsed
    specialist_plan = (
        await gateway.generate_structured(
            StructuredModelRequest(
                invocation_key="work_intelligence.vi.plan",
                messages=(ModelMessage(role="user", content="{}"),),
                output_schema=WorkStepPlan,
                timeout_seconds=5,
            )
        )
    ).parsed

    assert planning_plan.steps[0].target_agent_id is AgentId.PLANNING
    assert planning_plan.steps[0].capability == "planning.create"
    assert work_plan.steps[0].target_agent_id is AgentId.WORK_INTELLIGENCE
    assert work_plan.steps[0].capability == "work.read_my_tasks"
    assert specialist_plan.tool_id == "work.read_my_tasks"


@pytest.mark.asyncio
async def test_worker_persists_running_activity_before_first_model_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id, membership_id = uuid4(), uuid4()
    conversation = AssistantConversation.create(
        organization_id=organization_id,
        owner_membership_id=membership_id,
        locale="en",
    ).record_message(1)
    message = AssistantMessage(
        id=uuid4(),
        organization_id=organization_id,
        conversation_id=conversation.id,
        sequence=1,
        role=MessageRole.USER,
        content_blocks=({"kind": "text", "text": "What is next?"},),
        created_by_membership_id=membership_id,
    )
    turn = AssistantTurn.create(
        organization_id=organization_id,
        conversation_id=conversation.id,
        user_message_id=message.id,
        actor_membership_id=membership_id,
        objective="What is next?",
        locale="en",
    )
    run = OrchestrationRun.create(
        organization_id=organization_id,
        turn_id=turn.id,
        orchestrator_version="1.0.0",
        orchestrator_fingerprint="f" * 64,
        execution_plan={},
        budget={},
    ).mark_running()
    job = AssistantJob.create(
        organization_id=organization_id,
        conversation_id=conversation.id,
        turn_id=turn.id,
        orchestration_run_id=run.id,
        requester_membership_id=membership_id,
        payload={},
    )
    actor = AuthenticatedActor(
        user_id=uuid4(),
        email="employee@example.test",
        display_name="Employee",
        membership_id=membership_id,
        organization_id=organization_id,
        organization_name="Tenant",
        role=MembershipRole.EMPLOYEE,
    )
    snapshot = AssistantConversationSnapshot(
        conversation=conversation,
        messages=(message,),
        turns=(turn,),
        orchestration_runs=(run,),
        events=(),
    )

    class Repository:
        async def begin_orchestration(self, **_):
            return run

        async def get_conversation_snapshot(self, **_):
            return snapshot

        async def finish_orchestration(self, **_):
            return None

    class Transaction:
        repository = Repository()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def commit(self):
            return None

    class Recorder:
        def __init__(self) -> None:
            self.blocks: list[tuple[tuple[ResponseBlock, ...], str]] = []

        async def ensure_orchestrator_run(self):
            return uuid4()

        async def append_public_blocks(
            self,
            conversation_id: UUID,
            turn_id: UUID,
            blocks: tuple[ResponseBlock, ...],
            dedupe_key: str,
        ) -> None:
            assert conversation_id == conversation.id
            assert turn_id == turn.id
            self.blocks.append((blocks, dedupe_key))

        async def finish_orchestrator_run(self, *_):
            return None

        async def check_claim(self, *_):
            return None

    recorder = Recorder()

    def recorder_factory(**_: object) -> Recorder:
        return recorder

    monkeypatch.setattr(
        agent_runtime_module,
        "PostgreSQLExecutionRecorder",
        recorder_factory,
    )
    plan = ExecutionPlan(
        objectives=("Unavailable",),
        unavailable_capabilities=("reporting.generate",),
        response_language="en",
    )

    class Engine:
        async def execute(self, **_):
            return OrchestratorOutput(
                execution_plan=plan,
                agent_results=(),
                blocks=(),
                completed_step_ids=(),
                status=OrchestratorStatus.COMPLETED,
                stop_reason="CAPABILITY_UNAVAILABLE",
                replans_used=0,
                model_refs=(),
            )

    registry, _ = build_agent_registry()
    await AssistantTurnExecutor(
        transaction_factory=lambda _: Transaction(),  # type: ignore[arg-type]
        registry=registry,
        engine_factory=lambda _: Engine(),  # type: ignore[arg-type]
    ).execute_job(job=job, actor=actor)

    assert len(recorder.blocks) == 1
    blocks, dedupe_key = recorder.blocks[0]
    assert dedupe_key == f"assistant:{turn.id}:running"
    assert blocks[0].model_dump(mode="json") == {
        "kind": "activity",
        "label_key": "assistant.turn.running",
        "status": "RUNNING",
        "agent_id": "orchestrator",
    }
