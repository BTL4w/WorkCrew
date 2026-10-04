import importlib
import json
import sys
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

import pytest
from _pytest.monkeypatch import MonkeyPatch

from work_management_ai.agents.orchestrator.contracts import (
    ActiveConversationContext,
    ActivePlanningContext,
    ActiveTeamContext,
    ExactAssignmentContext,
    ExecutionPlan,
    OrchestratorInput,
    OrchestratorStatus,
)
from work_management_ai.agents.orchestrator.evaluators.plan import (
    ExecutionPlanError,
    ready_batches,
    validate_execution_plan,
)
from work_management_ai.agents.orchestrator.harness import OrchestratorHarness
from work_management_ai.agents.orchestrator.prompts import build_plan_messages
from work_management_ai.model_gateway.mock import MockModelGateway
from work_management_ai.runtime.agent_registry import AgentRegistry
from work_management_ai.runtime.contracts import (
    ActorReference,
    AgentHandoff,
    AgentId,
    AgentResult,
    AgentRunStatus,
    CapabilityUnavailableResponseBlock,
    QuestionResponseBlock,
    RequestedHandoff,
    ResolvedActorContext,
    SafeErrorResponseBlock,
)
from work_management_ai.runtime.manifests import (
    AgentManifest,
    SkillManifest,
    ToolManifest,
    load_yaml_resource,
)
from work_management_ai.runtime.policy_guard import PolicyGuard
from work_management_ai.runtime.skill_registry import SkillRegistry
from work_management_ai.runtime.tool_registry import ToolRegistry


class StaticActorResolver:
    def __init__(self, actor: ResolvedActorContext) -> None:
        self._actor = actor

    async def resolve(self, reference: ActorReference) -> ResolvedActorContext:
        assert reference.membership_id == self._actor.membership_id
        assert reference.organization_id == self._actor.organization_id
        return self._actor


class RecordingSpecialistRunner:
    def __init__(self, results: dict[str, list[AgentResult]] | None = None) -> None:
        self.handoffs: list[AgentHandoff] = []
        self._results = {key: list(values) for key, values in (results or {}).items()}

    async def run_specialist(self, handoff: AgentHandoff) -> AgentResult:
        self.handoffs.append(handoff)
        queued = self._results.get(handoff.capability)
        if queued:
            return queued.pop(0)
        return _completed_result(handoff.target_agent_id)


def _resolved_actor(
    role: Literal["ADMIN", "MANAGER", "EMPLOYEE"] = "MANAGER",
) -> ResolvedActorContext:
    return ResolvedActorContext(
        membership_id=uuid4(),
        organization_id=uuid4(),
        role=role,
        is_active=True,
    )


def _input(
    actor: ResolvedActorContext,
    *,
    locale: Literal["vi", "en"],
    message: str,
) -> OrchestratorInput:
    return OrchestratorInput(
        conversation_id=uuid4(),
        turn_id=uuid4(),
        message=message,
        locale=locale,
        actor=ActorReference(
            membership_id=actor.membership_id,
            organization_id=actor.organization_id,
        ),
        active_context=ActiveConversationContext(recent_messages=()),
    )


def _completed_result(agent_id: AgentId) -> AgentResult:
    return AgentResult(
        agent_id=agent_id,
        agent_version="1.0.0",
        status=AgentRunStatus.COMPLETED,
        typed_output={"summary": "verified result"},
        stop_reason="completed",
    )


def _requested_result(agent_id: AgentId, capability: str) -> AgentResult:
    return AgentResult(
        agent_id=agent_id,
        agent_version="1.0.0",
        status=AgentRunStatus.COMPLETED,
        typed_output={"summary": "needs another capability"},
        requested_handoff=RequestedHandoff(
            target_capability=capability,
            objective=f"Delegate {capability}",
            typed_input={"source": "specialist_result"},
        ),
        stop_reason="requested_handoff",
    )


def _awaiting_input_result(agent_id: AgentId) -> AgentResult:
    return AgentResult(
        agent_id=agent_id,
        agent_version="1.0.0",
        status=AgentRunStatus.AWAITING_INPUT,
        typed_output={"question": "Vui lòng xác nhận phạm vi chạy thử."},
        stop_reason="awaiting_input",
    )


def _fixture(name: str) -> dict[str, object]:
    path = Path(__file__).parents[5] / "tests" / "fixtures" / name
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def _write_specialist_manifest(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    *,
    package_name: str,
    agent_id: AgentId,
    capability: str,
    roles: tuple[str, ...],
    risk: str,
) -> tuple[str, str]:
    package = tmp_path / package_name
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    roles_yaml = ", ".join(roles)
    writes = "NEVER" if risk == "READ_ONLY" else "ALWAYS"
    (package / "agent.yaml").write_text(
        f"""schema_version: "1.0"
agent:
  id: {agent_id.value}
  name: Test {agent_id.value}
  version: "1.0.0"
  owner: test
  activation_phase: 2
capabilities: [{capability}]
contracts:
  input: work_management_ai.runtime.contracts.AgentHandoff
  output: work_management_ai.runtime.contracts.AgentResult
  handoff: work_management_ai.runtime.contracts.AgentHandoff
permissions:
  roles: [{roles_yaml}]
  tenant_scope: actor_membership
  risk_ceiling: {risk}
runtime:
  workflow: {agent_id.value}.v1
  max_iterations: 8
  max_tool_calls: 12
  max_handoffs: 0
  max_replans: 1
  timeout_seconds: 120
  checkpoint: durable
  model_policy: structured_reasoning
allowed_skills: []
allowed_tools: []
approval:
  produced_writes: {writes}
  can_self_approve: false
fallback:
  strategy: SAFE_FAILURE
evaluators: [{agent_id.value}_evaluator@1]
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    importlib.invalidate_caches()
    sys.modules.pop(package_name, None)
    return package_name, "agent.yaml"


def _registry(tmp_path: Path, monkeypatch: MonkeyPatch) -> AgentRegistry:
    registry = AgentRegistry(
        skill_registry=SkillRegistry(),
        tool_registry=ToolRegistry(),
        evaluator_ids=frozenset(
            {
                "orchestrator_plan@1",
                "work_intelligence_evaluator@1",
                "planning_evaluator@1",
            }
        ),
    )
    registry.register_resource("work_management_ai.agents.orchestrator", "agent.yaml")
    work_package = _write_specialist_manifest(
        tmp_path,
        monkeypatch,
        package_name="test_work_intelligence_agent",
        agent_id=AgentId.WORK_INTELLIGENCE,
        capability="work.read_project",
        roles=("ADMIN", "MANAGER", "EMPLOYEE"),
        risk="READ_ONLY",
    )
    planning_package = _write_specialist_manifest(
        tmp_path,
        monkeypatch,
        package_name="test_planning_agent",
        agent_id=AgentId.PLANNING,
        capability="planning.create",
        roles=("ADMIN", "MANAGER"),
        risk="PROPOSAL_ONLY",
    )
    registry.register_resource(*work_package)
    registry.register_resource(*planning_package)
    return registry


def _revision_registry(tmp_path: Path, monkeypatch: MonkeyPatch) -> AgentRegistry:
    registry = AgentRegistry(
        skill_registry=SkillRegistry(),
        tool_registry=ToolRegistry(),
        evaluator_ids=frozenset({"orchestrator_plan@1", "planning_evaluator@1"}),
    )
    registry.register_resource("work_management_ai.agents.orchestrator", "agent.yaml")
    planning_package = _write_specialist_manifest(
        tmp_path,
        monkeypatch,
        package_name="test_revision_planning_agent",
        agent_id=AgentId.PLANNING,
        capability="planning.revise",
        roles=("ADMIN", "MANAGER"),
        risk="PROPOSAL_ONLY",
    )
    registry.register_resource(*planning_package)
    return registry


def _phase3_registry() -> AgentRegistry:
    skill_packages = (
        "work_management_ai.skills.answer_work_question",
        "work_management_ai.skills.create_project_plan",
        "work_management_ai.skills.revise_project_plan",
        "work_management_ai.skills.recommend_project_team",
        "work_management_ai.skills.analyze_workload",
    )
    tool_packages = (
        "work_management_ai.tools.risk",
        "work_management_ai.tools.work.read_my_tasks",
        "work_management_ai.tools.work.read_resource",
        "work_management_ai.tools.planning.manage_run",
        "work_management_ai.tools.assignment.manage_team",
        "work_management_ai.tools.assignment.read_workload",
        "work_management_ai.tools.assignment.assign_task",
    )
    registry = AgentRegistry(
        skill_registry=SkillRegistry(
            load_yaml_resource(package, "skill.yaml", SkillManifest) for package in skill_packages
        ),
        tool_registry=ToolRegistry(
            load_yaml_resource(package, "tool.yaml", ToolManifest) for package in tool_packages
        ),
        evaluator_ids=frozenset(
            {
                "orchestrator_plan@1",
                "work_grounding@1",
                "planning_schema@1",
                "planning_invariants@1",
                "planning_grounding@1",
                "assignment_explanation@1",
                "assignment_policy@1",
            }
        ),
    )
    for package in (
        "work_management_ai.agents.orchestrator",
        "work_management_ai.agents.work_intelligence",
        "work_management_ai.agents.planning",
        "work_management_ai.agents.assignment",
    ):
        registry.register_resource(package, "agent.yaml")
    return registry


def _harness(
    *,
    actor: ResolvedActorContext,
    registry: AgentRegistry,
    runner: RecordingSpecialistRunner,
    fixtures: dict[str, object],
) -> OrchestratorHarness:
    return OrchestratorHarness(
        model_gateway=MockModelGateway(fixtures=fixtures, model_ref="mock:orchestrator-v1"),
        registry=registry,
        policy_guard=PolicyGuard(),
        actor_resolver=StaticActorResolver(actor),
        specialists=runner,
    )


def test_orchestrator_manifest_has_zero_business_tools() -> None:
    manifest = load_yaml_resource(
        "work_management_ai.agents.orchestrator", "agent.yaml", AgentManifest
    )

    assert manifest.agent.id is AgentId.ORCHESTRATOR
    assert manifest.agent.version == "1.0.0"
    assert manifest.allowed_tools == ()
    assert manifest.allowed_skills == ()
    assert manifest.runtime.max_tool_calls == 0
    assert manifest.runtime.max_handoffs == 6
    assert manifest.runtime.max_replans == 2


def test_active_proposal_context_does_not_imply_a_revision_request() -> None:
    value = ActivePlanningContext.model_validate(
        {
            "workflow_run_id": str(uuid4()),
            "workflow_status": "WAITING_FOR_DECISION",
            "proposal_id": str(uuid4()),
            "proposal_version": 1,
            "proposal_status": "READY_FOR_DECISION",
        }
    )

    assert value is not None
    assert value.requested_operation is None


def test_plan_prompt_exposes_only_registry_backed_specialist_capabilities(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    actor = _resolved_actor()
    registry = _registry(tmp_path, monkeypatch)

    messages = build_plan_messages(
        _input(actor, locale="vi", message="Lập kế hoạch project"),
        mode="plan",
        requested_handoff=None,
        prior_plan=None,
        specialist_catalog=registry.planning_catalog(active_phase=2, role=actor.role),
    )
    payload = json.loads(messages[1].content)

    assert payload["specialist_catalog"] == [
        {
            "agent_id": "planning",
            "agent_version": "1.0.0",
            "capabilities": ["planning.create"],
            "risk_ceiling": "PROPOSAL_ONLY",
        },
        {
            "agent_id": "work_intelligence",
            "agent_version": "1.0.0",
            "capabilities": ["work.read_project"],
            "risk_ceiling": "READ_ONLY",
        },
    ]
    assert "project_plan_proposal" not in messages[1].content


@pytest.mark.asyncio
async def test_single_work_intent_delegates_once(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    golden = _fixture("orchestrator_vi.json")
    actor = _resolved_actor(role="EMPLOYEE")
    runner = RecordingSpecialistRunner()
    harness = _harness(
        actor=actor,
        registry=_registry(tmp_path, monkeypatch),
        runner=runner,
        fixtures={
            "orchestrator.vi.plan": golden["plan"],
            "orchestrator.vi.synthesize": golden["synthesis"],
        },
    )

    output = await harness.run_turn(
        _input(actor, locale="vi", message=cast(str, golden["message"]))
    )

    assert output.status is OrchestratorStatus.COMPLETED
    assert [handoff.target_agent_id for handoff in runner.handoffs] == [AgentId.WORK_INTELLIGENCE]
    assert [block.kind for block in output.blocks] == ["text"]
    assert output.model_refs == ("mock:orchestrator-v1", "mock:orchestrator-v1")


@pytest.mark.asyncio
async def test_completed_specialist_cannot_synthesize_false_manager_input_interrupt(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    actor = _resolved_actor()
    runner = RecordingSpecialistRunner()
    workflow_run_id = uuid4()
    harness = _harness(
        actor=actor,
        registry=_registry(tmp_path, monkeypatch),
        runner=runner,
        fixtures={
            "orchestrator.vi.plan": {
                "schema_version": "1.0",
                "objectives": ["Create a weekly proposal"],
                "steps": [
                    {
                        "step_id": "plan_project",
                        "target_agent_id": "planning",
                        "target_agent_version": "1.0.0",
                        "capability": "planning.create",
                        "objective": "Create the proposal",
                        "typed_input": {},
                        "depends_on": [],
                        "mode": "PROPOSAL",
                    }
                ],
                "unavailable_capabilities": [],
                "response_language": "vi",
            },
            "orchestrator.vi.synthesize": {
                "blocks": [
                    {
                        "kind": "planning_run",
                        "workflow_run_id": str(workflow_run_id),
                        "status": "QUEUED",
                    },
                    {
                        "kind": "question",
                        "question": "Cần thêm thông tin trước khi lập proposal.",
                        "response_context": {"source": "model"},
                    },
                ]
            },
        },
    )

    output = await harness.run_turn(_input(actor, locale="vi", message="Lập kế hoạch theo tuần"))

    assert output.status is OrchestratorStatus.FAILED
    assert [block.kind for block in output.blocks] == ["safe_error"]


@pytest.mark.asyncio
async def test_specialist_awaiting_input_uses_deterministic_question_route(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    actor = _resolved_actor()
    runner = RecordingSpecialistRunner(
        {"planning.create": [_awaiting_input_result(AgentId.PLANNING)]}
    )
    harness = _harness(
        actor=actor,
        registry=_registry(tmp_path, monkeypatch),
        runner=runner,
        fixtures={
            "orchestrator.vi.plan": {
                "schema_version": "1.0",
                "objectives": ["Create a weekly proposal"],
                "steps": [
                    {
                        "step_id": "plan_project",
                        "target_agent_id": "planning",
                        "target_agent_version": "1.0.0",
                        "capability": "planning.create",
                        "objective": "Create the proposal",
                        "typed_input": {},
                        "depends_on": [],
                        "mode": "PROPOSAL",
                    }
                ],
                "unavailable_capabilities": [],
                "response_language": "vi",
            }
        },
    )

    output = await harness.run_turn(_input(actor, locale="vi", message="Lập kế hoạch theo tuần"))

    assert output.status is OrchestratorStatus.AWAITING_INPUT
    assert output.stop_reason == "AWAITING_INPUT"
    assert [block.kind for block in output.blocks] == ["question"]
    question = output.blocks[0]
    assert isinstance(question, QuestionResponseBlock)
    assert question.question == "Vui lòng xác nhận phạm vi chạy thử."
    assert output.model_refs == ("mock:orchestrator-v1",)


@pytest.mark.asyncio
async def test_manager_multi_intent_runs_work_then_planning(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    golden = _fixture("orchestrator_en.json")
    actor = _resolved_actor()
    runner = RecordingSpecialistRunner()
    harness = _harness(
        actor=actor,
        registry=_registry(tmp_path, monkeypatch),
        runner=runner,
        fixtures={
            "orchestrator.en.plan": golden["plan"],
            "orchestrator.en.synthesize": golden["synthesis"],
        },
    )

    output = await harness.run_turn(
        _input(actor, locale="en", message=cast(str, golden["message"]))
    )

    assert output.status is OrchestratorStatus.COMPLETED
    assert [handoff.target_agent_id for handoff in runner.handoffs] == [
        AgentId.WORK_INTELLIGENCE,
        AgentId.PLANNING,
    ]
    assert output.completed_step_ids == ("read_project", "plan_project")
    planning_handoff = runner.handoffs[1]
    assert planning_handoff.typed_input == {
        "operation": "CREATE",
        "locale": "en",
        "brief": cast(str, golden["message"]),
    }


@pytest.mark.asyncio
async def test_explicit_revision_card_deterministically_delegates_planning_revise(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    actor = _resolved_actor()
    workflow_run_id = uuid4()
    proposal_id = uuid4()
    runner = RecordingSpecialistRunner()
    harness = _harness(
        actor=actor,
        registry=_revision_registry(tmp_path, monkeypatch),
        runner=runner,
        fixtures={
            "orchestrator.vi.plan": {
                "schema_version": "1.0",
                "objectives": ["Create a new generic proposal"],
                "steps": [
                    {
                        "step_id": "create_plan",
                        "target_agent_id": "planning",
                        "target_agent_version": "1.0.0",
                        "capability": "planning.create",
                        "objective": "Create a proposal",
                        "typed_input": {},
                        "depends_on": [],
                        "mode": "PROPOSAL",
                    }
                ],
                "unavailable_capabilities": [],
                "response_language": "vi",
            },
            "orchestrator.vi.synthesize": {"blocks": [{"kind": "text", "text": "Đã nhận."}]},
        },
    )
    value = OrchestratorInput.model_validate(
        {
            "conversation_id": str(uuid4()),
            "turn_id": str(uuid4()),
            "message": "Mở rộng đến cuối tháng 11",
            "locale": "vi",
            "actor": {
                "membership_id": str(actor.membership_id),
                "organization_id": str(actor.organization_id),
            },
            "active_context": {
                "recent_messages": [],
                "active_planning": {
                    "workflow_run_id": str(workflow_run_id),
                    "workflow_status": "WAITING_FOR_DECISION",
                    "proposal_id": str(proposal_id),
                    "proposal_version": 1,
                    "proposal_status": "READY_FOR_DECISION",
                    "requested_operation": "REVISE",
                },
            },
        }
    )

    output = await harness.run_turn(value)

    assert output.status is OrchestratorStatus.COMPLETED
    assert len(runner.handoffs) == 1
    handoff = runner.handoffs[0]
    assert handoff.capability == "planning.revise"
    assert handoff.typed_input == {
        "operation": "REVISE",
        "workflow_run_id": str(workflow_run_id),
        "locale": "vi",
        "brief": "Mở rộng đến cuối tháng 11",
        "proposal_id": str(proposal_id),
        "expected_proposal_version": 1,
        "manager_instruction": "Mở rộng đến cuối tháng 11",
    }


@pytest.mark.asyncio
async def test_direct_chat_can_revise_the_active_proposal(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    actor = _resolved_actor()
    workflow_run_id = uuid4()
    proposal_id = uuid4()
    runner = RecordingSpecialistRunner()
    harness = _harness(
        actor=actor,
        registry=_revision_registry(tmp_path, monkeypatch),
        runner=runner,
        fixtures={
            "orchestrator.vi.plan": {
                "objectives": ["Mở rộng proposal hiện tại"],
                "steps": [
                    {
                        "step_id": "revise_plan",
                        "target_agent_id": "planning",
                        "target_agent_version": "1.0.0",
                        "capability": "planning.revise",
                        "objective": "Mở rộng proposal hiện tại đến cuối tháng 11",
                        "typed_input": {},
                        "mode": "PROPOSAL",
                    }
                ],
                "response_language": "vi",
            },
            "orchestrator.vi.synthesize": {
                "blocks": [{"kind": "text", "text": "Đã gửi proposal chỉnh sửa."}]
            },
        },
    )
    value = OrchestratorInput(
        conversation_id=uuid4(),
        turn_id=uuid4(),
        message="Mở rộng kế hoạch hiện tại đến cuối tháng 11",
        locale="vi",
        actor=ActorReference(
            membership_id=actor.membership_id,
            organization_id=actor.organization_id,
        ),
        active_context=ActiveConversationContext(
            recent_messages=(),
            active_planning=ActivePlanningContext(
                workflow_run_id=workflow_run_id,
                workflow_status="WAITING_FOR_DECISION",
                proposal_id=proposal_id,
                proposal_version=1,
                proposal_status="READY_FOR_DECISION",
            ),
        ),
    )

    output = await harness.run_turn(value)

    assert output is not None
    assert output.status is OrchestratorStatus.COMPLETED
    assert len(runner.handoffs) == 1
    assert runner.handoffs[0].typed_input == {
        "operation": "REVISE",
        "workflow_run_id": str(workflow_run_id),
        "locale": "vi",
        "brief": "Mở rộng kế hoạch hiện tại đến cuối tháng 11",
        "proposal_id": str(proposal_id),
        "expected_proposal_version": 1,
        "manager_instruction": "Mở rộng kế hoạch hiện tại đến cuối tháng 11",
    }


@pytest.mark.asyncio
async def test_plain_chat_cannot_mutate_an_active_proposal(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    actor = _resolved_actor()
    invalid_revision_plan: dict[str, object] = {
        "objectives": ["Respond to the greeting"],
        "steps": [
            {
                "step_id": "revise_plan",
                "target_agent_id": "planning",
                "target_agent_version": "1.0.0",
                "capability": "planning.revise",
                "objective": "Revise the active proposal",
                "typed_input": {},
                "mode": "PROPOSAL",
            }
        ],
        "response_language": "en",
    }
    runner = RecordingSpecialistRunner()
    harness = _harness(
        actor=actor,
        registry=_revision_registry(tmp_path, monkeypatch),
        runner=runner,
        fixtures={
            "orchestrator.en.plan": invalid_revision_plan,
            "orchestrator.en.repair": invalid_revision_plan,
            "orchestrator.en.synthesize": {"blocks": [{"kind": "text", "text": "Hello."}]},
        },
    )
    value = OrchestratorInput(
        conversation_id=uuid4(),
        turn_id=uuid4(),
        message="hello",
        locale="en",
        actor=ActorReference(
            membership_id=actor.membership_id,
            organization_id=actor.organization_id,
        ),
        active_context=ActiveConversationContext(
            recent_messages=(),
            active_planning=ActivePlanningContext(
                workflow_run_id=uuid4(),
                workflow_status="WAITING_FOR_DECISION",
                proposal_id=uuid4(),
                proposal_version=1,
                proposal_status="READY_FOR_DECISION",
            ),
        ),
    )

    output = await harness.run_turn(value)

    assert output.status is OrchestratorStatus.FAILED
    assert output.stop_reason == "EXECUTION_PLAN_INVALID"
    assert runner.handoffs == []


def test_independent_read_steps_form_one_parallel_batch() -> None:
    plan = ExecutionPlan.model_validate(
        {
            "objectives": ["Read two permitted resources"],
            "steps": [
                {
                    "step_id": "read_project",
                    "target_agent_id": "work_intelligence",
                    "target_agent_version": "1.0.0",
                    "capability": "work.read_project",
                    "objective": "Read Project A",
                    "typed_input": {"reference": "Project A"},
                    "mode": "READ_ONLY",
                },
                {
                    "step_id": "read_project_b",
                    "target_agent_id": "work_intelligence",
                    "target_agent_version": "1.0.0",
                    "capability": "work.read_project",
                    "objective": "Read Project B",
                    "typed_input": {"reference": "Project B"},
                    "mode": "READ_ONLY",
                },
            ],
            "response_language": "en",
        }
    )

    assert tuple(step.step_id for step in ready_batches(plan, frozenset())[0]) == (
        "read_project",
        "read_project_b",
    )


def test_proposal_step_never_runs_in_parallel() -> None:
    plan = ExecutionPlan.model_validate(
        {
            "objectives": ["Read and propose"],
            "steps": [
                {
                    "step_id": "read_project",
                    "target_agent_id": "work_intelligence",
                    "target_agent_version": "1.0.0",
                    "capability": "work.read_project",
                    "objective": "Read Project A",
                    "typed_input": {},
                    "mode": "READ_ONLY",
                },
                {
                    "step_id": "plan_project",
                    "target_agent_id": "planning",
                    "target_agent_version": "1.0.0",
                    "capability": "planning.create",
                    "objective": "Plan Project A",
                    "typed_input": {},
                    "mode": "PROPOSAL",
                },
            ],
            "response_language": "en",
        }
    )

    first = ready_batches(plan, frozenset())[0]
    second = ready_batches(plan, frozenset({"read_project"}))[0]
    assert tuple(step.step_id for step in first) == ("read_project",)
    assert tuple(step.step_id for step in second) == ("plan_project",)


def test_cycle_duplicate_step_and_unknown_agent_are_rejected(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    actor = _resolved_actor()
    registry = _registry(tmp_path, monkeypatch)
    common: dict[str, object] = {
        "target_agent_id": "work_intelligence",
        "target_agent_version": "1.0.0",
        "capability": "work.read_project",
        "objective": "Read",
        "typed_input": {},
        "mode": "READ_ONLY",
    }
    invalid_plans = (
        ExecutionPlan.model_validate(
            {
                "objectives": ["cycle"],
                "steps": [
                    {**common, "step_id": "a", "depends_on": ["b"]},
                    {**common, "step_id": "b", "depends_on": ["a"]},
                ],
                "response_language": "en",
            }
        ),
        ExecutionPlan.model_validate(
            {
                "objectives": ["duplicate"],
                "steps": [
                    {**common, "step_id": "same"},
                    {**common, "step_id": "same"},
                ],
                "response_language": "en",
            }
        ),
        ExecutionPlan.model_validate(
            {
                "objectives": ["unknown"],
                "steps": [
                    {
                        **common,
                        "step_id": "unknown",
                        "target_agent_id": "planning",
                        "target_agent_version": "9.0.0",
                        "capability": "planning.create",
                        "mode": "PROPOSAL",
                    }
                ],
                "response_language": "en",
            }
        ),
    )

    for plan in invalid_plans:
        with pytest.raises(ExecutionPlanError):
            validate_execution_plan(plan, registry, actor)


@pytest.mark.asyncio
async def test_requested_handoff_returns_to_orchestrator_before_new_delegation(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    actor = _resolved_actor()
    initial: dict[str, object] = {
        "schema_version": "1.0",
        "objectives": ["Create a grounded Project A proposal"],
        "steps": [
            {
                "step_id": "read_project",
                "target_agent_id": "work_intelligence",
                "target_agent_version": "1.0.0",
                "capability": "work.read_project",
                "objective": "Load permitted Project facts",
                "typed_input": {"reference": "Project A"},
                "depends_on": [],
                "mode": "READ_ONLY",
            }
        ],
        "unavailable_capabilities": [],
        "response_language": "vi",
    }
    replan: dict[str, object] = {
        **initial,
        "steps": [
            *cast(list[object], initial["steps"]),
            {
                "step_id": "plan_project",
                "target_agent_id": "planning",
                "target_agent_version": "1.0.0",
                "capability": "planning.create",
                "objective": "Create a proposal from the evidence",
                "typed_input": {"brief": "Plan Project A"},
                "depends_on": ["read_project"],
                "mode": "PROPOSAL",
            },
        ],
    }
    runner = RecordingSpecialistRunner(
        {"work.read_project": [_requested_result(AgentId.WORK_INTELLIGENCE, "planning.create")]}
    )
    harness = _harness(
        actor=actor,
        registry=_registry(tmp_path, monkeypatch),
        runner=runner,
        fixtures={
            "orchestrator.vi.plan": initial,
            "orchestrator.vi.replan.1": replan,
            "orchestrator.vi.synthesize": {
                "blocks": [{"kind": "text", "text": "Kế hoạch đã sẵn sàng."}]
            },
        },
    )

    output = await harness.run_turn(_input(actor, locale="vi", message="Lập kế hoạch Project A"))

    assert output.replans_used == 1
    assert [handoff.target_agent_id for handoff in runner.handoffs] == [
        AgentId.WORK_INTELLIGENCE,
        AgentId.PLANNING,
    ]
    assert runner.handoffs[1].parent_agent_run_id == runner.handoffs[0].parent_agent_run_id


@pytest.mark.asyncio
async def test_inactive_capability_yields_availability_block_and_zero_handoffs(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    actor = _resolved_actor()
    runner = RecordingSpecialistRunner()
    plan: dict[str, object] = {
        "schema_version": "1.0",
        "objectives": ["Generate a management report"],
        "steps": [],
        "unavailable_capabilities": ["reporting.generate"],
        "response_language": "en",
    }
    harness = _harness(
        actor=actor,
        registry=_registry(tmp_path, monkeypatch),
        runner=runner,
        fixtures={"orchestrator.en.plan": plan},
    )

    output = await harness.run_turn(
        _input(actor, locale="en", message="Generate a weekly management report")
    )

    assert runner.handoffs == []
    assert output.status is OrchestratorStatus.COMPLETED
    assert [block.kind for block in output.blocks] == ["capability_unavailable"]
    block = output.blocks[0]
    assert isinstance(block, CapabilityUnavailableResponseBlock)
    assert block.capability == "reporting.generate"


@pytest.mark.asyncio
async def test_malformed_model_plan_uses_safe_manual_fallback(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    actor = _resolved_actor()
    runner = RecordingSpecialistRunner()
    harness = _harness(
        actor=actor,
        registry=_registry(tmp_path, monkeypatch),
        runner=runner,
        fixtures={
            "orchestrator.vi.plan": {"steps": "invalid"},
            "orchestrator.vi.repair": {"steps": "still invalid"},
        },
    )

    output = await harness.run_turn(_input(actor, locale="vi", message="Giúp tôi lập kế hoạch"))

    assert runner.handoffs == []
    assert output.status is OrchestratorStatus.FAILED
    assert output.stop_reason == "MODEL_PLAN_INVALID"
    assert [block.kind for block in output.blocks] == ["safe_error"]
    block = output.blocks[0]
    assert isinstance(block, SafeErrorResponseBlock)
    assert block.code == "ORCHESTRATOR_MANUAL_FALLBACK"


@pytest.mark.asyncio
async def test_inactive_actor_stops_before_model_or_delegation(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    actor = _resolved_actor().model_copy(update={"is_active": False})
    runner = RecordingSpecialistRunner()
    harness = _harness(
        actor=actor,
        registry=_registry(tmp_path, monkeypatch),
        runner=runner,
        fixtures={
            "orchestrator.en.plan": _fixture("orchestrator_en.json")["plan"],
        },
    )

    output = await harness.run_turn(
        _input(actor, locale="en", message="Show the current project status")
    )

    assert output.status is OrchestratorStatus.FAILED
    assert output.stop_reason == "ACTOR_INACTIVE"
    assert output.execution_plan is None
    assert output.model_refs == ()
    assert runner.handoffs == []


@pytest.mark.asyncio
async def test_invalid_replan_never_reexecutes_the_prior_plan(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    actor = _resolved_actor()
    initial: dict[str, object] = {
        "schema_version": "1.0",
        "objectives": ["Create a grounded Project A proposal"],
        "steps": [
            {
                "step_id": "read_project",
                "target_agent_id": "work_intelligence",
                "target_agent_version": "1.0.0",
                "capability": "work.read_project",
                "objective": "Read Project A",
                "typed_input": {},
                "mode": "READ_ONLY",
            }
        ],
        "response_language": "en",
    }
    runner = RecordingSpecialistRunner(
        {"work.read_project": [_requested_result(AgentId.WORK_INTELLIGENCE, "planning.create")]}
    )
    harness = _harness(
        actor=actor,
        registry=_registry(tmp_path, monkeypatch),
        runner=runner,
        fixtures={
            "orchestrator.en.plan": initial,
            "orchestrator.en.replan.1": {"steps": "invalid"},
        },
    )

    output = await harness.run_turn(
        _input(actor, locale="en", message="Create a grounded Project A proposal")
    )

    assert output.status is OrchestratorStatus.FAILED
    assert output.stop_reason == "MODEL_PLAN_INVALID"
    assert output.execution_plan is None
    assert [handoff.capability for handoff in runner.handoffs] == ["work.read_project"]


@pytest.mark.asyncio
async def test_replan_budget_exhaustion_stops_without_broadening_scope(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    actor = _resolved_actor()
    runner = RecordingSpecialistRunner(
        {
            "work.read_project": [_requested_result(AgentId.WORK_INTELLIGENCE, "planning.create")],
            "planning.create": [
                _requested_result(AgentId.PLANNING, "planning.create"),
                _requested_result(AgentId.PLANNING, "planning.create"),
            ],
        }
    )
    initial: dict[str, object] = {
        "schema_version": "1.0",
        "objectives": ["Create a grounded Project A proposal"],
        "steps": [
            {
                "step_id": "read_project",
                "target_agent_id": "work_intelligence",
                "target_agent_version": "1.0.0",
                "capability": "work.read_project",
                "objective": "Read Project A",
                "typed_input": {},
                "mode": "READ_ONLY",
            }
        ],
        "response_language": "en",
    }
    replan_one: dict[str, object] = {
        **initial,
        "steps": [
            *cast(list[object], initial["steps"]),
            {
                "step_id": "plan_one",
                "target_agent_id": "planning",
                "target_agent_version": "1.0.0",
                "capability": "planning.create",
                "objective": "Create proposal",
                "typed_input": {},
                "depends_on": ["read_project"],
                "mode": "PROPOSAL",
            },
        ],
    }
    replan_two: dict[str, object] = {
        **replan_one,
        "steps": [
            *cast(list[object], replan_one["steps"]),
            {
                "step_id": "plan_two",
                "target_agent_id": "planning",
                "target_agent_version": "1.0.0",
                "capability": "planning.create",
                "objective": "Retry within approved scope",
                "typed_input": {},
                "depends_on": ["plan_one"],
                "mode": "PROPOSAL",
            },
        ],
    }
    harness = _harness(
        actor=actor,
        registry=_registry(tmp_path, monkeypatch),
        runner=runner,
        fixtures={
            "orchestrator.en.plan": initial,
            "orchestrator.en.replan.1": replan_one,
            "orchestrator.en.replan.2": replan_two,
        },
    )

    output = await harness.run_turn(
        _input(actor, locale="en", message="Create a grounded Project A proposal")
    )

    assert output.status is OrchestratorStatus.FAILED
    assert output.stop_reason == "REPLAN_BUDGET_EXHAUSTED"
    assert output.replans_used == 2
    assert [handoff.capability for handoff in runner.handoffs] == [
        "work.read_project",
        "planning.create",
        "planning.create",
    ]
    assert output.blocks[0].kind == "safe_error"


@pytest.mark.asyncio
async def test_project_only_prompt_never_delegates_to_assignment() -> None:
    actor = _resolved_actor()
    runner = RecordingSpecialistRunner()
    plan: dict[str, object] = {
        "objectives": ["Create Project Atlas"],
        "steps": [
            {
                "step_id": "plan_project",
                "target_agent_id": "planning",
                "target_agent_version": "1.0.0",
                "capability": "planning.create",
                "objective": "Create Project Atlas",
                "typed_input": {},
                "mode": "PROPOSAL",
            }
        ],
        "response_language": "en",
    }
    harness = _harness(
        actor=actor,
        registry=_phase3_registry(),
        runner=runner,
        fixtures={
            "orchestrator.en.plan": plan,
            "orchestrator.en.synthesize": {"blocks": [{"kind": "text", "text": "Ready."}]},
        },
    )

    await harness.run_turn(_input(actor, locale="en", message="Create Project Atlas"))

    assert [handoff.target_agent_id for handoff in runner.handoffs] == [AgentId.PLANNING]


@pytest.mark.asyncio
async def test_project_only_prompt_rejects_model_fabricated_assignment_step() -> None:
    actor = _resolved_actor()
    runner = RecordingSpecialistRunner()
    fabricated: dict[str, object] = {
        "objectives": ["Create Project Atlas"],
        "steps": [
            {
                "step_id": "plan_project",
                "target_agent_id": "planning",
                "target_agent_version": "1.0.0",
                "capability": "planning.create",
                "objective": "Create Project Atlas",
                "typed_input": {},
                "mode": "PROPOSAL",
            },
            {
                "step_id": "recommend_team",
                "target_agent_id": "assignment",
                "target_agent_version": "1.0.0",
                "capability": "assignment.recommend_team",
                "objective": "Recommend a team",
                "typed_input": {},
                "depends_on": ["plan_project"],
                "mode": "PROPOSAL",
            },
        ],
        "response_language": "en",
    }
    harness = _harness(
        actor=actor,
        registry=_phase3_registry(),
        runner=runner,
        fixtures={
            "orchestrator.en.plan": fabricated,
            "orchestrator.en.repair": fabricated,
        },
    )

    output = await harness.run_turn(_input(actor, locale="en", message="Create Project Atlas"))

    assert output.status is OrchestratorStatus.FAILED
    assert output.stop_reason == "EXECUTION_PLAN_INVALID"
    assert runner.handoffs == []


@pytest.mark.asyncio
async def test_historical_team_context_cannot_authorize_unrelated_assignment() -> None:
    actor = _resolved_actor()
    runner = RecordingSpecialistRunner()
    fabricated: dict[str, object] = {
        "objectives": ["Analyze the old team"],
        "steps": [
            {
                "step_id": "analyze_workload",
                "target_agent_id": "assignment",
                "target_agent_version": "1.0.0",
                "capability": "assignment.analyze_workload",
                "objective": "Analyze workload",
                "typed_input": {},
                "mode": "READ_ONLY",
            }
        ],
        "response_language": "en",
    }
    harness = _harness(
        actor=actor,
        registry=_phase3_registry(),
        runner=runner,
        fixtures={
            "orchestrator.en.plan": fabricated,
            "orchestrator.en.repair": fabricated,
        },
    )
    value = _input(actor, locale="en", message="Summarize my latest conversation").model_copy(
        update={
            "active_context": ActiveConversationContext(
                recent_messages=(), active_team=ActiveTeamContext(project_id=uuid4())
            )
        }
    )

    output = await harness.run_turn(value)

    assert output.status is OrchestratorStatus.FAILED
    assert output.stop_reason == "EXECUTION_PLAN_INVALID"
    assert runner.handoffs == []


@pytest.mark.asyncio
async def test_team_revision_card_uses_exact_trusted_context() -> None:
    actor = _resolved_actor()
    project_id = uuid4()
    recommendation_id = uuid4()
    runner = RecordingSpecialistRunner(
        {
            "assignment.revise_team": [
                AgentResult(
                    agent_id=AgentId.ASSIGNMENT,
                    agent_version="1.0.0",
                    status=AgentRunStatus.AWAITING_HUMAN,
                    typed_output={"summary": "review team proposal"},
                    stop_reason="awaiting_human",
                )
            ]
        }
    )
    harness = _harness(
        actor=actor,
        registry=_phase3_registry(),
        runner=runner,
        fixtures={"orchestrator.vi.synthesize": {"blocks": [{"kind": "text", "text": "Đã nhận."}]}},
    )
    value = _input(actor, locale="vi", message="Thay Lan bằng Minh vì Lan quá tải").model_copy(
        update={
            "active_context": ActiveConversationContext(
                recent_messages=(),
                active_team=ActiveTeamContext(
                    project_id=project_id,
                    recommendation_id=recommendation_id,
                    recommendation_version=2,
                    recommendation_status="PROPOSED",
                    requested_operation="REVISE_TEAM",
                ),
            )
        }
    )

    output = await harness.run_turn(value)

    assert output.status is OrchestratorStatus.AWAITING_HUMAN
    assert len(runner.handoffs) == 1
    handoff = runner.handoffs[0]
    assert handoff.target_agent_id is AgentId.ASSIGNMENT
    assert handoff.capability == "assignment.revise_team"
    assert handoff.typed_input == {
        "operation": "REVISE_TEAM",
        "locale": "vi",
        "project_id": str(project_id),
        "recommendation_id": str(recommendation_id),
        "recommendation_version": 2,
        "revision_instruction": "Thay Lan bằng Minh vì Lan quá tải",
    }


@pytest.mark.asyncio
async def test_exact_assignment_context_allows_one_explicit_write_handoff() -> None:
    actor = _resolved_actor()
    project_id, task_id, membership_id = uuid4(), uuid4(), uuid4()
    runner = RecordingSpecialistRunner()
    harness = _harness(
        actor=actor,
        registry=_phase3_registry(),
        runner=runner,
        fixtures={
            "orchestrator.en.synthesize": {
                "blocks": [{"kind": "text", "text": "The task was assigned."}]
            }
        },
    )
    value = _input(actor, locale="en", message="Assign Task A to Lan").model_copy(
        update={
            "active_context": ActiveConversationContext(
                recent_messages=(),
                exact_assignment=ExactAssignmentContext(
                    project_id=project_id,
                    task_id=task_id,
                    task_version=4,
                    membership_id=membership_id,
                ),
            )
        }
    )

    output = await harness.run_turn(value)

    assert output.status is OrchestratorStatus.COMPLETED
    assert len(runner.handoffs) == 1
    handoff = runner.handoffs[0]
    assert handoff.capability == "assignment.assign_task_explicitly"
    assert handoff.typed_input == {
        "operation": "ASSIGN_TASK_EXPLICITLY",
        "locale": "en",
        "task_id": str(task_id),
        "task_version": 4,
        "membership_id": str(membership_id),
    }


@pytest.mark.asyncio
async def test_ambiguous_explicit_assignment_asks_before_any_handoff() -> None:
    actor = _resolved_actor()
    runner = RecordingSpecialistRunner()
    harness = _harness(actor=actor, registry=_phase3_registry(), runner=runner, fixtures={})
    value = _input(actor, locale="en", message="Assign Task A to Lan").model_copy(
        update={
            "active_context": ActiveConversationContext(
                recent_messages=(),
                assignment_resolution_issue="TASK_AMBIGUOUS_OR_NOT_FOUND",
            )
        }
    )

    output = await harness.run_turn(value)

    assert output.status is OrchestratorStatus.AWAITING_INPUT
    assert runner.handoffs == []
    assert isinstance(output.blocks[0], QuestionResponseBlock)
    assert output.blocks[0].response_context == {"source": "assignment_resolution"}


@pytest.mark.asyncio
async def test_employee_cannot_use_exact_assignment_context() -> None:
    actor = _resolved_actor(role="EMPLOYEE")
    runner = RecordingSpecialistRunner()
    harness = _harness(
        actor=actor,
        registry=_phase3_registry(),
        runner=runner,
        fixtures={},
    )
    value = _input(actor, locale="en", message="Assign Task A to Lan").model_copy(
        update={
            "active_context": ActiveConversationContext(
                recent_messages=(),
                exact_assignment=ExactAssignmentContext(
                    project_id=uuid4(),
                    task_id=uuid4(),
                    task_version=1,
                    membership_id=uuid4(),
                ),
            )
        }
    )

    output = await harness.run_turn(value)

    assert output.status is OrchestratorStatus.COMPLETED
    assert output.stop_reason == "CAPABILITY_UNAVAILABLE"
    assert runner.handoffs == []


@pytest.mark.asyncio
async def test_employee_assignment_resolution_denial_never_calls_a_specialist() -> None:
    actor = _resolved_actor(role="EMPLOYEE")
    runner = RecordingSpecialistRunner()
    harness = _harness(actor=actor, registry=_phase3_registry(), runner=runner, fixtures={})
    value = _input(actor, locale="en", message="Assign Task A to Lan").model_copy(
        update={
            "active_context": ActiveConversationContext(
                recent_messages=(), assignment_resolution_issue="ASSIGNMENT_FORBIDDEN"
            )
        }
    )

    output = await harness.run_turn(value)

    assert output.status is OrchestratorStatus.COMPLETED
    assert output.stop_reason == "ASSIGNMENT_FORBIDDEN"
    assert runner.handoffs == []
    assert isinstance(output.blocks[0], CapabilityUnavailableResponseBlock)


@pytest.mark.asyncio
async def test_combined_project_team_plan_emits_durable_pending_followup() -> None:
    actor = _resolved_actor()
    workflow_run_id, proposal_id = uuid4(), uuid4()
    planning_result = AgentResult(
        agent_id=AgentId.PLANNING,
        agent_version="1.0.0",
        status=AgentRunStatus.AWAITING_HUMAN,
        typed_output={
            "operation": "CREATE",
            "workflow_run_id": str(workflow_run_id),
            "workflow_status": "WAITING_FOR_DECISION",
            "proposal_id": str(proposal_id),
            "proposal_version": 2,
            "approval_id": str(uuid4()),
            "awaiting": "MANAGER_DECISION",
            "public_summary": "Review the project proposal.",
        },
        stop_reason="awaiting_human",
    )
    runner = RecordingSpecialistRunner({"planning.create": [planning_result]})
    harness = _harness(
        actor=actor,
        registry=_phase3_registry(),
        runner=runner,
        fixtures={
            "orchestrator.en.plan": {
                "objectives": ["Create Project Atlas and form its team"],
                "steps": [
                    {
                        "step_id": "plan_project",
                        "target_agent_id": "planning",
                        "target_agent_version": "1.0.0",
                        "capability": "planning.create",
                        "objective": "Create Project Atlas",
                        "typed_input": {},
                        "mode": "PROPOSAL",
                    },
                    {
                        "step_id": "recommend_team",
                        "target_agent_id": "assignment",
                        "target_agent_version": "1.0.0",
                        "capability": "assignment.recommend_team",
                        "objective": "Recommend the Project Team after approval",
                        "typed_input": {},
                        "depends_on": ["plan_project"],
                        "mode": "PROPOSAL",
                    },
                ],
                "response_language": "en",
            }
        },
    )

    output = await harness.run_turn(
        _input(actor, locale="en", message="Create Project Atlas and form a team")
    )

    assert [handoff.capability for handoff in runner.handoffs] == ["planning.create"]
    assert output.pending_followup is not None
    assert output.pending_followup.planning_workflow_run_id == workflow_run_id
    assert output.pending_followup.planning_proposal_id == proposal_id
    assert output.pending_followup.planning_proposal_version == 2
    assert output.pending_followup.state == "WAITING_PROJECT_DECISION"


@pytest.mark.asyncio
async def test_async_combined_plan_waits_for_proposal_before_assignment() -> None:
    actor = _resolved_actor()
    workflow_run_id = uuid4()
    planning_result = AgentResult(
        agent_id=AgentId.PLANNING,
        agent_version="1.0.0",
        status=AgentRunStatus.COMPLETED,
        typed_output={
            "operation": "CREATE",
            "workflow_run_id": str(workflow_run_id),
            "workflow_status": "QUEUED",
            "proposal_id": None,
            "proposal_version": None,
            "approval_id": None,
            "awaiting": "NONE",
            "public_summary": "Planning workflow started.",
        },
        stop_reason="COMPLETED",
    )
    runner = RecordingSpecialistRunner({"planning.create": [planning_result]})
    harness = _harness(
        actor=actor,
        registry=_phase3_registry(),
        runner=runner,
        fixtures={
            "orchestrator.en.plan": {
                "objectives": ["Create Project Atlas and form its team"],
                "steps": [
                    {
                        "step_id": "plan_project",
                        "target_agent_id": "planning",
                        "target_agent_version": "1.0.0",
                        "capability": "planning.create",
                        "objective": "Create Project Atlas",
                        "typed_input": {},
                        "mode": "PROPOSAL",
                    },
                    {
                        "step_id": "recommend_team",
                        "target_agent_id": "assignment",
                        "target_agent_version": "1.0.0",
                        "capability": "assignment.recommend_team",
                        "objective": "Recommend the Project Team after approval",
                        "typed_input": {},
                        "depends_on": ["plan_project"],
                        "mode": "PROPOSAL",
                    },
                ],
                "response_language": "en",
            }
        },
    )

    output = await harness.run_turn(
        _input(actor, locale="en", message="Create Project Atlas and form a team")
    )

    assert [handoff.capability for handoff in runner.handoffs] == ["planning.create"]
    assert output.status is OrchestratorStatus.AWAITING_HUMAN
    assert output.pending_followup is not None
    assert output.pending_followup.planning_workflow_run_id == workflow_run_id
    assert output.pending_followup.state == "WAITING_PROJECT_PROPOSAL"


@pytest.mark.asyncio
async def test_model_cannot_fabricate_assignment_result_block() -> None:
    actor = _resolved_actor()
    runner = RecordingSpecialistRunner()
    harness = _harness(
        actor=actor,
        registry=_phase3_registry(),
        runner=runner,
        fixtures={
            "orchestrator.en.plan": {
                "objectives": ["Read my tasks"],
                "steps": [
                    {
                        "step_id": "read_tasks",
                        "target_agent_id": "work_intelligence",
                        "target_agent_version": "1.0.0",
                        "capability": "work.read_my_tasks",
                        "objective": "Read tasks",
                        "typed_input": {},
                        "mode": "READ_ONLY",
                    }
                ],
                "response_language": "en",
            },
            "orchestrator.en.synthesize": {
                "blocks": [
                    {
                        "kind": "assignment_result",
                        "task_id": str(uuid4()),
                        "task_version": 2,
                        "membership_id": str(uuid4()),
                        "warning_codes": [],
                    }
                ]
            },
        },
    )

    output = await harness.run_turn(_input(actor, locale="en", message="Read my tasks"))

    assert output.status is OrchestratorStatus.FAILED
    assert all(block.kind != "assignment_result" for block in output.blocks)


@pytest.mark.asyncio
async def test_post_project_team_failure_links_to_manual_team_editor() -> None:
    actor = _resolved_actor()
    runner = RecordingSpecialistRunner(
        {
            "assignment.recommend_team": [
                AgentResult(
                    agent_id=AgentId.ASSIGNMENT,
                    agent_version="1.0.0",
                    status=AgentRunStatus.FAILED,
                    typed_output={"fallback": "manual_assignment"},
                    stop_reason="tool_failed",
                    safe_error_code="ASSIGNMENT_MANUAL_FALLBACK",
                )
            ]
        }
    )
    harness = _harness(
        actor=actor,
        registry=_phase3_registry(),
        runner=runner,
        fixtures={},
    )
    value = _input(actor, locale="en", message="Continue with the Project Team").model_copy(
        update={
            "active_context": ActiveConversationContext(
                recent_messages=(),
                active_team=ActiveTeamContext(
                    project_id=uuid4(), requested_operation="RECOMMEND_TEAM"
                ),
            )
        }
    )

    output = await harness.run_turn(value)

    assert output.status is OrchestratorStatus.FAILED
    block = output.blocks[0]
    assert isinstance(block, SafeErrorResponseBlock)
    assert block.manual_fallback == "PROJECT_TEAM_EDITOR"
