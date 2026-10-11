"""Bilingual actual-graph evaluation; database evidence is opt-in and fail-closed."""

import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from work_management_ai.agents.orchestrator.contracts import (
    ActiveConversationContext,
    ActivePlanningContext,
    OrchestratorInput,
)
from work_management_ai.agents.orchestrator.harness import OrchestratorHarness
from work_management_ai.agents.planning.contracts import PlanningAgentInput, PlanningOperation
from work_management_ai.evaluation.phase2_multi_agent import build_evaluation_registry
from work_management_ai.model_gateway.contracts import (
    ModelGateway,
    StructuredModelRequest,
    StructuredModelResponse,
)
from work_management_ai.model_gateway.mock import MockModelGateway
from work_management_ai.runtime.contracts import (
    ActorReference,
    AgentHandoff,
    AgentId,
    AgentResult,
    AgentRunStatus,
    InteractionOutcome,
    ResolvedActorContext,
)
from work_management_ai.runtime.policy_guard import PolicyGuard
from work_management_ai.schemas.progressive_planning import (
    DraftBinding,
    PlanningDecision,
    PlanningDraft,
)
from work_management_ai.workflows.planning.context import (
    PermittedPlanningContext,
    PlanningContextRequest,
)
from work_management_ai.workflows.planning.draft_verifier import DraftValidationResult
from work_management_ai.workflows.planning.progressive_graph import run_progressive
from work_management_ai.workflows.planning.state import (
    ProgressivePlanningResult,
    ProgressivePlanningState,
)

SCENARIOS = {
    "ask-goal",
    "course-draft",
    "decisive",
    "update-budget",
    "reduce-budget",
    "new-plan",
    "conflict",
    "refresh",
    "stale-approval",
    "permissions",
}
ROOT = Path(__file__).resolve().parents[4]


class EvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    scenario: str
    locale: Literal["vi", "en"]
    message: str
    expected: str
    layer: Literal["GRAPH", "POSTGRES"]
    active: bool
    base: dict[str, Any] | None
    candidate: dict[str, Any] | None


class EvaluationObservation(BaseModel):
    case_id: str
    scenario: str
    locale: Literal["vi", "en"]
    mode: Literal["MOCK", "HOSTED", "POSTGRES", "NOT_RUN"]
    status: Literal["PASS", "FAIL", "NOT_RUN"]
    decision: str | None = None
    run_id: UUID | None = None
    draft_id: UUID | None = None
    draft_version: int | None = None
    model_calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    model_refs: tuple[str, ...] = ()
    assertions: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()


class ProgressivePlanningEvaluationReport(BaseModel):
    mode: Literal["MOCK", "HOSTED"]
    observations: tuple[EvaluationObservation, ...]
    coverage_complete: bool
    graph_passed: bool
    passed: bool
    hosted_status: Literal["NOT_RUN", "MEASURED"]


def load_cases() -> list[EvaluationCase]:
    raw = json.loads(
        (ROOT / "ai/tests/fixtures/progressive_planning/evaluation_cases.json").read_text()
    )
    return [EvaluationCase.model_validate(row) for row in raw]


class MockProgressiveEvaluationGateway:
    """Return literal independent fixtures; run actual harness/graph/verifiers."""

    case: EvaluationCase | None = None

    async def generate_structured[T: BaseModel](
        self, request: StructuredModelRequest[T]
    ) -> StructuredModelResponse[T]:
        if self.case is None:
            raise ValueError("evaluation case is not selected")
        fixture: dict[str, Any] | None
        if request.invocation_key.startswith("orchestrator."):
            resume = self.case.scenario in {"update-budget", "reduce-budget", "conflict"}
            fixture = {
                "objectives": [self.case.message],
                "steps": [
                    {
                        "step_id": "planning",
                        "target_agent_id": "planning",
                        "target_agent_version": "1.0.0",
                        "capability": "planning.resume" if resume else "planning.create",
                        "objective": self.case.message,
                        "typed_input": {},
                        "mode": "PROPOSAL",
                    }
                ],
                "planning_intent": "CONTINUE_DRAFT" if resume else "START_NEW",
                "response_language": self.case.locale,
            }
        elif request.invocation_key.endswith(".grounding"):
            fixture = {
                "passed": True,
                "unsupported_claim_paths": [],
                "constraint_violation_paths": [],
                "safe_reasons": [],
            }
        else:
            fixture = self.case.candidate
        return await MockModelGateway(
            fixtures={request.invocation_key: fixture}, model_ref="mock:progressive-golden-v1"
        ).generate_structured(request)


class ObservedGateway:
    def __init__(self, gateway: ModelGateway) -> None:
        self.gateway = gateway
        self.calls = 0
        self.inputs = 0
        self.outputs = 0
        self.usage_known = True
        self.refs: list[str] = []

    async def generate_structured[T: BaseModel](
        self, request: StructuredModelRequest[T]
    ) -> StructuredModelResponse[T]:
        self.calls += 1
        response = await self.gateway.generate_structured(request)
        self.refs.append(response.model_ref)
        if response.usage is None:
            self.usage_known = False
        else:
            self.inputs += response.usage.input_tokens
            self.outputs += response.usage.output_tokens
        return response


class EvaluationContext:
    async def load_permitted_context(
        self, request: PlanningContextRequest
    ) -> PermittedPlanningContext:
        return PermittedPlanningContext(
            ("message:prior", "message:edit"),
            (),
            {"input": request.user_brief, "current_message_ref": "message:edit"},
        )


class EvaluationPersistence:
    def __init__(self, base: PlanningDraft | None, binding: DraftBinding | None) -> None:
        self.base = base
        self.binding = binding
        self.published: list[PlanningDraft] = []

    async def publish_progressive_result(
        self,
        state: ProgressivePlanningState,
        decision: PlanningDecision,
        validation: DraftValidationResult,
    ) -> DraftBinding | None:
        if decision.draft is None:
            return state.current_binding
        self.published.append(decision.draft)
        return DraftBinding(
            draft_id=state.current_binding.draft_id if state.current_binding else uuid4(),
            draft_version=state.current_binding.draft_version + 1 if state.current_binding else 1,
            fingerprint=hashlib.sha256(decision.draft.model_dump_json().encode()).hexdigest(),
        )


class EvaluationActor:
    def __init__(self) -> None:
        self.actor = ResolvedActorContext(
            membership_id=uuid4(), organization_id=uuid4(), role="MANAGER", is_active=True
        )

    async def resolve(self, reference: ActorReference) -> ResolvedActorContext:
        if (
            reference.membership_id != self.actor.membership_id
            or reference.organization_id != self.actor.organization_id
        ):
            raise PermissionError("actor mismatch")
        return self.actor


class GraphSpecialist:
    def __init__(
        self,
        gateway: ModelGateway,
        case: EvaluationCase,
        base: PlanningDraft | None,
        binding: DraftBinding | None,
    ) -> None:
        self.gateway, self.case, self.base, self.binding = gateway, case, base, binding
        self.result: ProgressivePlanningResult | None = None
        self.run_id = uuid4()

    async def run_specialist(self, handoff: AgentHandoff) -> AgentResult:
        if handoff.target_agent_id is not AgentId.PLANNING:
            raise ValueError("unexpected delegation")
        value = PlanningAgentInput.model_validate(handoff.typed_input)
        continuing = value.operation is PlanningOperation.UPDATE_DRAFT
        self.result = await run_progressive(
            ProgressivePlanningState(
                run_id=self.run_id,
                organization_id=handoff.actor.organization_id,
                actor_membership_id=handoff.actor.membership_id,
                actor_role="MANAGER",
                locale=value.locale,
                user_brief=value.brief,
                current_draft=self.base if continuing else None,
                current_binding=self.binding if continuing else None,
            ),
            gateway=self.gateway,
            context_port=EvaluationContext(),
            persistence=EvaluationPersistence(self.base, self.binding),
        )
        if self.result.safe_error_code or self.result.decision is None:
            return AgentResult(
                agent_id=AgentId.PLANNING,
                agent_version=handoff.target_agent_version,
                status=AgentRunStatus.FAILED,
                typed_output={},
                interaction_outcome=InteractionOutcome.FAILED,
                stop_reason=self.result.safe_error_code or "FAILED",
            )
        decision = self.result.decision
        if decision.draft is None:
            assert decision.question is not None
            return AgentResult(
                agent_id=AgentId.PLANNING,
                agent_version=handoff.target_agent_version,
                status=AgentRunStatus.AWAITING_INPUT,
                typed_output={
                    "question": decision.question.text,
                    "workflow_run_id": str(self.run_id),
                    "response_context": {"workflow_run_id": str(self.run_id), "progressive": True},
                },
                interaction_outcome=InteractionOutcome.NEEDS_INPUT,
                stop_reason="NEEDS_INPUT",
            )
        assert self.result.binding is not None
        return AgentResult(
            agent_id=AgentId.PLANNING,
            agent_version=handoff.target_agent_version,
            status=AgentRunStatus.COMPLETED,
            typed_output={
                "workflow_run_id": str(self.run_id),
                "workflow_status": "DRAFT_AVAILABLE",
                "draft_id": str(self.result.binding.draft_id),
                "draft_version": self.result.binding.draft_version,
                "awaiting": "NONE",
                "public_summary": str(decision.draft.brief.objective.value),
            },
            interaction_outcome=InteractionOutcome.DRAFT_READY,
            model_attempts_used=self.result.model_calls_used,
            stop_reason="DRAFT_READY",
        )


async def run_progressive_planning_suite(
    cases: list[EvaluationCase],
    gateway: ModelGateway,
    *,
    boundary_results: dict[tuple[str, str], bool] | None = None,
) -> ProgressivePlanningEvaluationReport:
    mode: Literal["MOCK", "HOSTED"] = (
        "MOCK" if isinstance(gateway, MockProgressiveEvaluationGateway) else "HOSTED"
    )
    observations: list[EvaluationObservation] = []
    for case in cases:
        if case.layer == "POSTGRES":
            measured = (case.scenario, case.locale) in (boundary_results or {})
            observations.append(
                EvaluationObservation(
                    case_id=case.case_id,
                    scenario=case.scenario,
                    locale=case.locale,
                    mode="POSTGRES" if measured else "NOT_RUN",
                    status=(
                        "PASS"
                        if (boundary_results or {}).get((case.scenario, case.locale))
                        else "FAIL"
                    )
                    if measured
                    else "NOT_RUN",
                    assertions=("POSTGRES_RLS_VERSIONS_AND_SIDE_EFFECTS",) if measured else (),
                )
            )
            continue
        if isinstance(gateway, MockProgressiveEvaluationGateway):
            gateway.case = case
        observed = ObservedGateway(gateway)
        actor = EvaluationActor()
        base = PlanningDraft.model_validate(case.base) if case.base else None
        binding = (
            DraftBinding(draft_id=uuid4(), draft_version=1, fingerprint="a" * 64) if base else None
        )
        original = base.model_dump_json() if base else None
        runner = GraphSpecialist(observed, case, base, binding)
        active = (
            ActivePlanningContext(
                workflow_run_id=uuid4(),
                workflow_status="DRAFT_AVAILABLE",
                workflow_version="3.0.0",
                proposal_id=None,
                draft_id=binding.draft_id if binding else None,
                draft_version=1,
            )
            if case.active
            else None
        )
        harness = OrchestratorHarness(
            model_gateway=observed,
            registry=build_evaluation_registry(),
            policy_guard=PolicyGuard(),
            actor_resolver=actor,
            specialists=runner,
        )
        started = time.monotonic()
        violations: list[str] = []
        assertions: list[str] = []
        try:
            output = await harness.run_turn(
                OrchestratorInput(
                    conversation_id=uuid4(),
                    turn_id=uuid4(),
                    message=case.message,
                    locale=case.locale,
                    actor=ActorReference(
                        membership_id=actor.actor.membership_id,
                        organization_id=actor.actor.organization_id,
                    ),
                    active_context=ActiveConversationContext(
                        recent_messages=(), active_planning=active
                    ),
                )
            )
            result = runner.result
            assert result is not None and result.decision is not None, output.stop_reason
            assert result.decision.kind == case.expected
            decision = result.decision
            assertions.append("EXPECTED_DECISION")
            if decision.draft is None:
                assert (
                    result.binding is None
                    and decision.question is not None
                    and decision.question.blocking
                )
                assert len(output.blocks) == 1 and output.blocks[0].kind == "question"
                assertions.append("ONE_QUESTION_NO_DRAFT_OR_APPROVAL")
            else:
                assert result.binding is not None
                assert any(block.kind == "planning_draft" for block in output.blocks)
                assert not any(
                    action.requires_human_gate
                    for answer in output.agent_results
                    for action in answer.proposed_actions
                )
                assertions.append("BOUND_DRAFT_WITHOUT_BUSINESS_GATE")
                if case.scenario == "course-draft":
                    assert "100" in str(decision.draft.brief.objective.value)
                    assert (
                        decision.draft.brief.timing.status == "AI_ASSUMPTION"
                        and len(decision.draft.phases) == 4
                    )
                    assert decision.question is not None and not decision.question.blocking
                    assertions.append("USEFUL_EARLY_DRAFT_WITH_ASSUMPTIONS")
                if case.scenario in {"update-budget", "reduce-budget"}:
                    assert (
                        binding is not None
                        and result.binding.draft_id == binding.draft_id
                        and result.binding.draft_version == 2
                    )
                    assert (
                        decision.draft.brief.budget is not None
                        and decision.draft.brief.budget.amount
                        == (20000000 if case.scenario == "update-budget" else 10000000)
                    )
                    assert (
                        base is not None
                        and decision.draft.brief.objective == base.brief.objective
                        and decision.draft.brief.audience == base.brief.audience
                        and decision.draft.duration_days == 42
                    )
                    assertions.append("EXACT_CURRENT_DRAFT_BUDGET_AND_PRESERVED_CONSTRAINTS")
                if case.scenario == "new-plan":
                    assert binding is not None and result.binding.draft_id != binding.draft_id
                    assert base is not None and base.model_dump_json() == original
                    assertions.append("NEW_IDENTITY_OLD_CONTENT_UNCHANGED")
                if case.scenario == "conflict":
                    assert not result.validation.can_prepare
                    conflict = result.validation.conflicts[0]
                    assert (
                        conflict.requested_days == 14
                        and conflict.minimum_days == 21
                        and conflict.chain_refs == ("t1", "t2", "t3")
                        and conflict.estimated
                    )
                    assertions.append("CRITICAL_PATH_21_DAYS_BLOCKS_PREPARATION")
        except (AssertionError, ValueError, KeyError, TypeError):
            violations.append("PROGRESSIVE_ACCEPTANCE_FAILED")
        result = runner.result
        observations.append(
            EvaluationObservation(
                case_id=case.case_id,
                scenario=case.scenario,
                locale=case.locale,
                mode=mode,
                status="FAIL" if violations else "PASS",
                decision=result.decision.kind if result and result.decision else None,
                run_id=runner.run_id,
                draft_id=result.binding.draft_id if result and result.binding else None,
                draft_version=result.binding.draft_version if result and result.binding else None,
                model_calls=observed.calls,
                input_tokens=observed.inputs if observed.usage_known else None,
                output_tokens=observed.outputs if observed.usage_known else None,
                latency_ms=int((time.monotonic() - started) * 1000),
                model_refs=tuple(observed.refs),
                assertions=tuple(assertions),
                violations=tuple(violations),
            )
        )
    required = {(scenario, locale) for scenario in SCENARIOS for locale in ("vi", "en")}
    covered = {(row.scenario, row.locale) for row in observations}
    complete = covered == required and len(observations) == 20
    graph_passed = all(
        row.status == "PASS" for row in observations if row.mode in {"MOCK", "HOSTED"}
    ) and {
        (row.scenario, row.locale) for row in observations if row.mode in {"MOCK", "HOSTED"}
    } == {
        (scenario, locale)
        for scenario in SCENARIOS - {"refresh", "stale-approval", "permissions"}
        for locale in ("vi", "en")
    }
    return ProgressivePlanningEvaluationReport(
        mode=mode,
        observations=tuple(observations),
        coverage_complete=complete,
        graph_passed=graph_passed,
        passed=complete and all(row.status == "PASS" for row in observations),
        hosted_status="MEASURED" if mode == "HOSTED" else "NOT_RUN",
    )


def measure_postgres_boundaries() -> dict[tuple[str, str], bool]:
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        return {}
    with tempfile.TemporaryDirectory(prefix="progressive-eval-") as folder:
        junit = Path(folder) / "results.xml"
        process = subprocess.run(
            [
                "uv",
                "run",
                "--project",
                str(ROOT / "backend"),
                "pytest",
                "tests/test_progressive_acceptance_boundaries.py",
                "-q",
                f"--junitxml={junit}",
            ],
            cwd=ROOT / "backend",
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if not junit.exists():
            return {}
        mapping = {
            "test_refresh_boundary": "refresh",
            "test_stale_approval_boundary": "stale-approval",
            "test_permission_boundary": "permissions",
        }
        result: dict[tuple[str, str], bool] = {}
        for node in ET.parse(junit).findall(".//testcase"):
            name = node.attrib.get("name", "")
            for prefix, scenario in mapping.items():
                for locale in ("vi", "en"):
                    if name == f"{prefix}[{locale}]":
                        result[(scenario, locale)] = process.returncode == 0 and not any(
                            node.find(kind) is not None for kind in ("failure", "error", "skipped")
                        )
        return result


async def evaluate_default_suite() -> ProgressivePlanningEvaluationReport:
    evidence = await asyncio.to_thread(measure_postgres_boundaries)
    return await run_progressive_planning_suite(
        load_cases(), MockProgressiveEvaluationGateway(), boundary_results=evidence
    )


if __name__ == "__main__":
    report = asyncio.run(evaluate_default_suite())
    print(report.model_dump_json())
    raise SystemExit(0 if report.passed else 1)
