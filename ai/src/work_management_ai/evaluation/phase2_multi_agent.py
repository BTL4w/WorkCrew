"""Deterministic runtime evaluation for the three Phase 2 activated Agents."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field

from work_management_ai.agents.orchestrator.contracts import (
    ActiveConversationContext,
    OrchestratorInput,
)
from work_management_ai.agents.orchestrator.harness import OrchestratorHarness
from work_management_ai.model_gateway.mock import MockModelGateway
from work_management_ai.runtime.agent_registry import AgentRegistry
from work_management_ai.runtime.contracts import (
    ActorReference,
    AgentHandoff,
    AgentId,
    AgentResult,
    AgentRunStatus,
    ContextReference,
    JsonValue,
    RequestedHandoff,
    ResolvedActorContext,
    VerifierResult,
)
from work_management_ai.runtime.manifests import SkillManifest, ToolManifest, load_yaml_resource
from work_management_ai.runtime.policy_guard import PolicyGuard
from work_management_ai.runtime.skill_registry import SkillRegistry
from work_management_ai.runtime.tool_registry import ToolRegistry


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Phase2EvaluationCase(_FrozenModel):
    """A redacted runtime input and its expected delegation boundary."""

    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,127}$")
    locale: Literal["vi", "en"]
    scenario: Literal[
        "work_single_intent",
        "planning_single_intent",
        "work_then_planning",
        "ambiguity",
        "insufficient_evidence",
        "requested_handoff",
        "inactive_capability",
        "provider_timeout",
        "invalid_structured_output",
        "prompt_tool_injection",
        "employee_planning_attempt",
        "cross_tenant_reference",
    ]
    expected_agent_path: tuple[AgentId, ...] = Field(min_length=1)
    fallback_expected: bool


class Phase2EvaluationObservation(_FrozenModel):
    case: Phase2EvaluationCase
    observed_agent_path: tuple[AgentId, ...]
    handoff_parent_agent_ids: tuple[AgentId, ...]
    answer_grounded: bool
    policy_violation: bool
    unsupported_claim: bool
    duplicate_side_effects: int = Field(ge=0)
    fallback_succeeded: bool


class Phase2EvaluationReport(_FrozenModel):
    total: int
    delegation_correct: int
    grounded_answers: int
    valid_handoffs: int
    policy_violations: int
    unsupported_claims: int
    duplicate_side_effects: int
    fallback_successes: int

    @property
    def passed(self) -> bool:
        return (
            self.delegation_correct == self.total
            and self.valid_handoffs == self.total
            and self.policy_violations == 0
            and self.unsupported_claims == 0
            and self.duplicate_side_effects == 0
        )


_SKILLS = (
    ("work_management_ai.skills.answer_work_question", "skill.yaml"),
    ("work_management_ai.skills.create_project_plan", "skill.yaml"),
    ("work_management_ai.skills.revise_project_plan", "skill.yaml"),
)
_TOOLS = (
    ("work_management_ai.tools.risk", "tool.yaml"),
    ("work_management_ai.tools.work.read_my_tasks", "tool.yaml"),
    ("work_management_ai.tools.work.read_resource", "tool.yaml"),
    ("work_management_ai.tools.planning.manage_run", "tool.yaml"),
)
_AGENTS = (
    ("work_management_ai.agents.orchestrator", "agent.yaml"),
    ("work_management_ai.agents.work_intelligence", "agent.yaml"),
    ("work_management_ai.agents.planning", "agent.yaml"),
)
_EVALUATORS = frozenset(
    {
        "orchestrator_plan@1",
        "work_grounding@1",
        "planning_schema@1",
        "planning_invariants@1",
        "planning_grounding@1",
    }
)
_SAFE_FALLBACKS = {
    "ambiguity": {"MODEL_PLAN_INVALID"},
    "insufficient_evidence": {"AWAITING_INPUT"},
    "inactive_capability": {"CAPABILITY_UNAVAILABLE"},
    "provider_timeout": {"SPECIALIST_RESULT_FAILED"},
    "invalid_structured_output": {"SPECIALIST_RESULT_FAILED"},
}


def _registry() -> AgentRegistry:
    skills = SkillRegistry(
        load_yaml_resource(package, resource, SkillManifest) for package, resource in _SKILLS
    )
    tools = ToolRegistry(
        load_yaml_resource(package, resource, ToolManifest) for package, resource in _TOOLS
    )
    registry = AgentRegistry(
        skill_registry=skills,
        tool_registry=tools,
        evaluator_ids=_EVALUATORS,
    )
    for package, resource in _AGENTS:
        registry.register_resource(package, resource)
    return registry


class _ActorResolver:
    def __init__(self, actor: ResolvedActorContext) -> None:
        self.actor = actor

    async def resolve(self, reference: ActorReference) -> ResolvedActorContext:
        if (
            reference.membership_id != self.actor.membership_id
            or reference.organization_id != self.actor.organization_id
        ):
            return self.actor.model_copy(update={"is_active": False})
        return self.actor


class _RuntimeSpecialists:
    """Deterministic specialist boundary used by the real Orchestrator Harness."""

    def __init__(self, *, case: Phase2EvaluationCase, actor: ResolvedActorContext) -> None:
        self.case = case
        self.actor = actor
        self.handoffs: list[AgentHandoff] = []
        self.results: list[AgentResult] = []

    async def run_specialist(self, handoff: AgentHandoff) -> AgentResult:
        self.handoffs.append(handoff)
        scenario = self.case.scenario
        if scenario == "requested_handoff" and len(self.handoffs) == 1:
            result = self._result(
                handoff,
                requested_handoff=RequestedHandoff(
                    target_capability="planning.create",
                    objective="Create a bounded proposal from verified work evidence",
                    typed_input={"source": "verified_work_result"},
                ),
            )
        elif scenario == "insufficient_evidence":
            result = self._result(
                handoff,
                status=AgentRunStatus.AWAITING_INPUT,
                typed_output={"question": "Please identify the Project."},
                stop_reason="INSUFFICIENT_EVIDENCE",
            )
        elif scenario in {"provider_timeout", "invalid_structured_output"}:
            result = self._result(
                handoff,
                status=AgentRunStatus.FAILED,
                typed_output={},
                stop_reason="SAFE_MANUAL_FALLBACK",
                safe_error_code=(
                    "MODEL_TIMEOUT" if scenario == "provider_timeout" else "MODEL_INVALID_OUTPUT"
                ),
            )
        elif scenario == "cross_tenant_reference":
            result = self._result(
                handoff,
                status=AgentRunStatus.FAILED,
                typed_output={},
                stop_reason="TENANT_REFERENCE_REJECTED",
                safe_error_code="WORK_MANUAL_READ_FALLBACK",
            )
        else:
            result = self._result(handoff)
        self.results.append(result)
        return result

    def _result(
        self,
        handoff: AgentHandoff,
        *,
        status: AgentRunStatus = AgentRunStatus.COMPLETED,
        typed_output: dict[str, JsonValue] | None = None,
        stop_reason: str = "COMPLETED",
        safe_error_code: str | None = None,
        requested_handoff: RequestedHandoff | None = None,
    ) -> AgentResult:
        evidence = ContextReference(
            reference_id=uuid5(
                NAMESPACE_URL, f"eval-evidence:{self.case.case_id}:{len(self.handoffs)}"
            ),
            organization_id=self.actor.organization_id,
            resource_type="EVALUATION_FIXTURE",
            resource_id=uuid5(NAMESPACE_URL, f"eval-resource:{self.case.case_id}"),
            version=1,
            observed_at=datetime(2026, 8, 24, tzinfo=UTC),
        )
        return AgentResult(
            agent_id=handoff.target_agent_id,
            agent_version=handoff.target_agent_version,
            status=status,
            typed_output=typed_output or {"summary": "Verified deterministic fixture"},
            evidence=(evidence,),
            verifier_results=(
                VerifierResult(
                    verifier_id="phase2_runtime_fixture",
                    verifier_version="1.0.0",
                    passed=True,
                ),
            ),
            requested_handoff=requested_handoff,
            stop_reason=stop_reason,
            safe_error_code=safe_error_code,
        )


def _step(agent: AgentId, *, step_id: str, depends_on: tuple[str, ...] = ()) -> dict[str, object]:
    planning = agent is AgentId.PLANNING
    return {
        "step_id": step_id,
        "target_agent_id": agent.value,
        "target_agent_version": "1.0.0",
        "capability": "planning.create" if planning else "work.answer_question",
        "objective": "Create a proposal" if planning else "Answer from verified work facts",
        "typed_input": {},
        "depends_on": list(depends_on),
        "mode": "PROPOSAL" if planning else "READ_ONLY",
    }


def _plan(case: Phase2EvaluationCase) -> dict[str, object]:
    scenario = case.scenario
    steps: list[dict[str, object]] = []
    unavailable: list[str] = []
    if scenario in {
        "work_single_intent",
        "insufficient_evidence",
        "provider_timeout",
        "cross_tenant_reference",
        "requested_handoff",
    }:
        steps = [_step(AgentId.WORK_INTELLIGENCE, step_id="read_work")]
    elif scenario in {"planning_single_intent", "invalid_structured_output"}:
        steps = [_step(AgentId.PLANNING, step_id="create_plan")]
    elif scenario == "work_then_planning":
        steps = [
            _step(AgentId.WORK_INTELLIGENCE, step_id="read_work"),
            _step(AgentId.PLANNING, step_id="create_plan", depends_on=("read_work",)),
        ]
    elif scenario == "inactive_capability":
        unavailable = ["assignment.recommend"]
    elif scenario in {"prompt_tool_injection", "employee_planning_attempt"}:
        steps = [_step(AgentId.PLANNING, step_id="forbidden_plan")]
    return {
        "objectives": [f"Evaluate {case.case_id}"],
        "steps": steps,
        "unavailable_capabilities": unavailable,
        "response_language": case.locale,
    }


def _fixtures(case: Phase2EvaluationCase) -> dict[str, object]:
    if case.scenario == "ambiguity":
        return {}
    plan = _plan(case)
    fixtures: dict[str, object] = {
        f"orchestrator.{case.locale}.plan": plan,
        f"orchestrator.{case.locale}.synthesize": {
            "blocks": [{"kind": "text", "text": "Verified deterministic response."}]
        },
    }
    if case.scenario in {"prompt_tool_injection", "employee_planning_attempt"}:
        fixtures[f"orchestrator.{case.locale}.repair"] = {
            "objectives": plan["objectives"],
            "steps": [],
            "unavailable_capabilities": ["planning.create"],
            "response_language": case.locale,
        }
    if case.scenario == "requested_handoff":
        fixtures[f"orchestrator.{case.locale}.replan.1"] = {
            **plan,
            "steps": [
                _step(AgentId.WORK_INTELLIGENCE, step_id="read_work"),
                _step(AgentId.PLANNING, step_id="create_plan", depends_on=("read_work",)),
            ],
        }
    return fixtures


async def _observe(case: Phase2EvaluationCase) -> Phase2EvaluationObservation:
    namespace = uuid5(NAMESPACE_URL, f"phase2-eval:{case.case_id}")
    role: Literal["MANAGER", "EMPLOYEE"] = (
        "EMPLOYEE"
        if case.scenario in {"prompt_tool_injection", "employee_planning_attempt"}
        else "MANAGER"
    )
    actor = ResolvedActorContext(
        membership_id=uuid5(namespace, "membership"),
        organization_id=uuid5(namespace, "organization"),
        role=role,
        is_active=True,
    )
    turn_id = uuid5(namespace, "turn")
    specialists = _RuntimeSpecialists(case=case, actor=actor)
    output = await OrchestratorHarness(
        model_gateway=MockModelGateway(fixtures=_fixtures(case), model_ref="mock:phase2-eval-v1"),
        registry=_registry(),
        policy_guard=PolicyGuard(),
        actor_resolver=_ActorResolver(actor),
        specialists=specialists,
    ).run_turn(
        OrchestratorInput(
            orchestration_run_id=uuid5(namespace, "orchestration"),
            conversation_id=uuid5(namespace, "conversation"),
            turn_id=turn_id,
            message=f"Redacted evaluation request {case.case_id}",
            locale=case.locale,
            actor=ActorReference(
                membership_id=actor.membership_id,
                organization_id=actor.organization_id,
            ),
            active_context=ActiveConversationContext(recent_messages=()),
        )
    )
    root_run_id = uuid5(NAMESPACE_URL, f"orchestrator:{turn_id}")
    parents = tuple(
        AgentId.ORCHESTRATOR
        for handoff in specialists.handoffs
        if handoff.parent_agent_run_id == root_run_id
    )
    paths = (AgentId.ORCHESTRATOR, *(handoff.target_agent_id for handoff in specialists.handoffs))
    counts = Counter(handoff.idempotency_key for handoff in specialists.handoffs)
    duplicates = sum(count - 1 for count in counts.values() if count > 1)
    policy_violation = any(
        handoff.actor.organization_id != actor.organization_id
        or handoff.actor.membership_id != actor.membership_id
        or (role == "EMPLOYEE" and handoff.target_agent_id is AgentId.PLANNING)
        for handoff in specialists.handoffs
    )
    fallback_codes = _SAFE_FALLBACKS.get(case.scenario, set())
    fallback_succeeded = case.fallback_expected and output.stop_reason in fallback_codes
    runtime_results = tuple(specialists.results)
    successful = tuple(
        result
        for result in runtime_results
        if result.status not in {AgentRunStatus.FAILED, AgentRunStatus.CANCELLED}
    )
    verified = all(
        result.evidence
        and all(item.organization_id == actor.organization_id for item in result.evidence)
        and result.verifier_results
        and all(item.passed for item in result.verifier_results)
        for result in successful
    )
    safe_failure = bool(runtime_results) and all(
        result.status is AgentRunStatus.FAILED and result.safe_error_code is not None
        for result in runtime_results
    )
    answer_grounded = (
        (verified and bool(successful))
        or fallback_succeeded
        or safe_failure
        or (not runtime_results and output.stop_reason == "CAPABILITY_UNAVAILABLE")
    )
    unsupported_claim = any(
        result.status is AgentRunStatus.COMPLETED
        and (not result.evidence or not all(item.passed for item in result.verifier_results))
        for result in runtime_results
    )
    return Phase2EvaluationObservation(
        case=case,
        observed_agent_path=paths,
        handoff_parent_agent_ids=parents,
        answer_grounded=answer_grounded,
        policy_violation=policy_violation,
        unsupported_claim=unsupported_claim,
        duplicate_side_effects=duplicates,
        fallback_succeeded=fallback_succeeded,
    )


def _has_valid_handoffs(observation: Phase2EvaluationObservation) -> bool:
    expected = len(observation.observed_agent_path) - 1
    return len(observation.handoff_parent_agent_ids) == expected and all(
        parent is AgentId.ORCHESTRATOR for parent in observation.handoff_parent_agent_ids
    )


async def _evaluate(cases: Iterable[Phase2EvaluationCase]) -> Phase2EvaluationReport:
    observations = tuple([await _observe(case) for case in cases])
    return Phase2EvaluationReport(
        total=len(observations),
        delegation_correct=sum(
            item.case.expected_agent_path == item.observed_agent_path for item in observations
        ),
        grounded_answers=sum(item.answer_grounded for item in observations),
        valid_handoffs=sum(_has_valid_handoffs(item) for item in observations),
        policy_violations=sum(item.policy_violation for item in observations),
        unsupported_claims=sum(item.unsupported_claim for item in observations),
        duplicate_side_effects=sum(item.duplicate_side_effects for item in observations),
        fallback_successes=sum(
            item.case.fallback_expected and item.fallback_succeeded for item in observations
        ),
    )


def evaluate_phase2_multi_agent(
    cases: Iterable[Phase2EvaluationCase],
) -> Phase2EvaluationReport:
    """Execute every case through the activated Orchestrator runtime."""

    return asyncio.run(_evaluate(tuple(cases)))


def load_phase2_cases(path: Path) -> tuple[Phase2EvaluationCase, ...]:
    cases: list[Phase2EvaluationCase] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSONL at line {line_number}") from error
        cases.append(Phase2EvaluationCase.model_validate(raw))
    return tuple(cases)


def _default_suite_path() -> Path:
    return Path(__file__).parents[3] / "evaluations/phase2_multi_agent.jsonl"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=_default_suite_path())
    args = parser.parse_args(argv)
    report = evaluate_phase2_multi_agent(load_phase2_cases(args.path))
    print(json.dumps(report.model_dump(), sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
