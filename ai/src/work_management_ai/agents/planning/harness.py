"""Guarded proposal-only Planning Specialist Agent Harness."""

from typing import cast
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from work_management_ai.agents.orchestrator.contracts import ActorContextResolverPort
from work_management_ai.agents.planning.contracts import (
    PlanningAgentInput,
    PlanningAgentOutput,
    PlanningOperation,
    PlanningStepPlan,
)
from work_management_ai.agents.planning.evaluators.proposal import (
    PlanningResultError,
    verify_planning_result,
)
from work_management_ai.agents.planning.prompts import build_step_plan_messages
from work_management_ai.agents.planning.skills import PlanningSkillLoader
from work_management_ai.agents.planning.workflows.graph import (
    PlanningAgentGraph,
    PlanningAgentState,
)
from work_management_ai.model_gateway.contracts import (
    ModelGateway,
    ModelRequestLimits,
    StructuredModelRequest,
)
from work_management_ai.model_gateway.errors import ModelGatewayError
from work_management_ai.runtime.contracts import (
    AgentHandoff,
    AgentId,
    AgentResult,
    AgentRunStatus,
    InteractionOutcome,
    JsonValue,
    ProposedAction,
    RiskLevel,
    ToolExecutionRequest,
    ToolExecutorPort,
    VerifierResult,
)
from work_management_ai.runtime.manifests import AgentManifest, ToolManifest, load_yaml_resource

_AGENT_PACKAGE = "work_management_ai.agents.planning"
_TOOL_PACKAGE = "work_management_ai.tools.planning.manage_run"
_MANUAL_FALLBACK_CODE = "PLANNING_MANUAL_EDITABLE_FALLBACK"
_CAPABILITY_BY_OPERATION = {
    PlanningOperation.CREATE: "planning.create",
    PlanningOperation.RESUME_INPUT: "planning.resume",
    PlanningOperation.REVISE: "planning.revise",
    PlanningOperation.EXPLAIN: "planning.explain",
    PlanningOperation.UPDATE_DRAFT: "planning.resume",
    PlanningOperation.PREPARE_ACTION: "planning.resume",
}


class PlanningAgentHarness:
    def __init__(
        self,
        *,
        model_gateway: ModelGateway,
        tool_executor: ToolExecutorPort,
        actor_resolver: ActorContextResolverPort,
    ) -> None:
        self._model_gateway = model_gateway
        self._tool_executor = tool_executor
        self._actor_resolver = actor_resolver
        self._manifest = load_yaml_resource(_AGENT_PACKAGE, "agent.yaml", AgentManifest)
        self._skill_loader = PlanningSkillLoader()
        self._graph = PlanningAgentGraph(self)

    async def run(self, handoff: AgentHandoff) -> AgentResult:
        return await self._graph.run(
            PlanningAgentState(
                handoff=handoff,
                actor=None,
                value=None,
                selected_skill=None,
                skill_instructions="",
                plan=None,
                tool_result=None,
                output=None,
                result=None,
                route="execute",
                stop_reason="NOT_STARTED",
                safe_error_code=None,
                iterations_used=0,
                tool_calls_used=0,
                model_refs=(),
            )
        )

    async def receive_handoff(self, state: PlanningAgentState) -> dict[str, object]:
        return {"stop_reason": "RUNNING"}

    async def validate_contract(self, state: PlanningAgentState) -> dict[str, object]:
        handoff = state["handoff"]
        runtime = self._manifest.runtime
        if (
            handoff.target_agent_id is not AgentId.PLANNING
            or handoff.target_agent_version != self._manifest.agent.version
            or handoff.capability not in self._manifest.capabilities
        ):
            return self._failure("PLANNING_HANDOFF_INVALID")
        if (
            handoff.budget.max_iterations > runtime.max_iterations
            or handoff.budget.max_tool_calls > runtime.max_tool_calls
            or handoff.budget.max_handoffs > runtime.max_handoffs
            or handoff.budget.max_replans > runtime.max_replans
            or handoff.budget.timeout_seconds > runtime.timeout_seconds
        ):
            return self._failure("PLANNING_BUDGET_INVALID")
        try:
            actor = await self._actor_resolver.resolve(handoff.actor)
        except Exception:
            return self._failure("PLANNING_ACTOR_NOT_FOUND")
        if (
            not actor.is_active
            or actor.membership_id != handoff.actor.membership_id
            or actor.organization_id != handoff.actor.organization_id
        ):
            return self._failure("PLANNING_ACTOR_NOT_FOUND")
        if actor.role not in self._manifest.permissions.roles:
            return self._failure("PLANNING_ROLE_FORBIDDEN", preserve_code=True)
        try:
            value = PlanningAgentInput.model_validate(handoff.typed_input)
        except ValidationError:
            return self._failure("PLANNING_INPUT_INVALID", preserve_code=True)
        if handoff.capability != _CAPABILITY_BY_OPERATION[value.operation]:
            return self._failure("PLANNING_CAPABILITY_MISMATCH")
        return {"actor": actor, "value": value, "route": "execute"}

    async def build_planning_context(self, state: PlanningAgentState) -> dict[str, object]:
        if any(
            reference.organization_id != state["handoff"].actor.organization_id
            for reference in state["handoff"].context_references
        ):
            return self._failure("PLANNING_CONTEXT_TENANT_MISMATCH")
        value = state["value"]
        if value is not None and value.retrieval_context_refs:
            expected = tuple(
                (ref.evidence_id, ref.fingerprint) for ref in value.retrieval_context_refs
            )
            actual = tuple(
                (ref.resource_id, ref.fingerprint)
                for ref in state["handoff"].context_references
                if ref.resource_type == "WEB_SOURCE_SNAPSHOT" and ref.version == 1
            )
            if expected != actual:
                return self._failure("PLANNING_EVIDENCE_BINDING_INVALID")
        return {"route": "execute"}

    async def select_create_or_revision_skill(self, state: PlanningAgentState) -> dict[str, object]:
        value = state["value"]
        if value is None:
            return self._failure("PLANNING_INPUT_INVALID", preserve_code=True)
        selected = (
            "revise_project_plan@1"
            if value.operation is PlanningOperation.REVISE
            else "create_project_plan@1"
        )
        try:
            _, instructions = self._skill_loader.load(selected)
        except (ValueError, ValidationError):
            return self._failure("PLANNING_SKILL_INVALID")
        if selected not in self._manifest.allowed_skills:
            return self._failure("PLANNING_SKILL_NOT_ALLOWED")
        return {
            "selected_skill": selected,
            "skill_instructions": instructions,
            "route": "execute",
        }

    async def create_step_plan(self, state: PlanningAgentState) -> dict[str, object]:
        value = state["value"]
        selected = state["selected_skill"]
        if value is None or selected is None:
            return self._failure("PLANNING_STATE_INVALID")
        if value.progressive or value.operation is PlanningOperation.REVISE:
            return {
                "plan": PlanningStepPlan(
                    skill_reference="revise_project_plan@1"
                    if selected == "revise_project_plan@1"
                    else "create_project_plan@1",
                    tool_id="planning.manage_run",
                    tool_input={},
                    requested_handoff=None,
                ),
                "route": "execute",
                "stop_reason": "RUNNING",
            }
        if state["iterations_used"] >= state["handoff"].budget.max_iterations:
            return self._failure("PLANNING_ITERATION_BUDGET_EXHAUSTED")
        request = StructuredModelRequest(
            invocation_key=f"planning_agent.{value.locale}.step_plan",
            messages=build_step_plan_messages(
                value,
                selected_skill=selected,
                skill_instructions=state["skill_instructions"],
                skill_catalog=self._skill_loader.catalog(),
                context_references=state["handoff"].context_references,
            ),
            output_schema=PlanningStepPlan,
            max_output_tokens=600 if value.progressive else None,
            limits=ModelRequestLimits(max_text_bytes=4000, max_output_tokens=600)
            if value.progressive
            else None,
            timeout_seconds=self._manifest.runtime.timeout_seconds,
        )
        try:
            response = await self._model_gateway.generate_structured(request)
        except ModelGatewayError:
            return self._failure("PLANNING_MODEL_FAILED")
        plan = response.parsed
        if plan.requested_handoff is not None:
            route = "requested_handoff"
        elif plan.skill_reference != selected:
            return self._failure("PLANNING_SKILL_MISMATCH")
        else:
            route = "execute"
        return {
            "plan": plan,
            "iterations_used": state["iterations_used"] + 1,
            "model_refs": (*state["model_refs"], response.model_ref),
            "route": route,
            "stop_reason": "REQUESTED_HANDOFF" if route == "requested_handoff" else "RUNNING",
        }

    async def execute_planning_tool(self, state: PlanningAgentState) -> dict[str, object]:
        plan = state["plan"]
        value = state["value"]
        selected = state["selected_skill"]
        if plan is None or value is None or selected is None or plan.tool_id is None:
            return self._failure("PLANNING_TOOL_PLAN_INVALID")
        if state["tool_calls_used"] >= state["handoff"].budget.max_tool_calls:
            return self._failure("PLANNING_TOOL_BUDGET_EXHAUSTED")
        tool_reference = f"{plan.tool_id}@1"
        if tool_reference not in self._manifest.allowed_tools:
            return self._failure("PLANNING_TOOL_NOT_ALLOWED")
        try:
            skill, _ = self._skill_loader.load(selected)
            tool_manifest = load_yaml_resource(_TOOL_PACKAGE, "tool.yaml", ToolManifest)
        except (ValueError, ValidationError):
            return self._failure("PLANNING_TOOL_NOT_ALLOWED")
        if (
            tool_reference not in skill.allowed_tools
            or tool_manifest.name != plan.tool_id
            or tool_manifest.risk_level is not RiskLevel.PROPOSAL_ONLY
        ):
            return self._failure("PLANNING_TOOL_NOT_ALLOWED")

        # Never trust model-produced authority or mutation fields.
        trusted_input = cast(dict[str, JsonValue], value.model_dump(mode="json"))
        request = ToolExecutionRequest(
            agent_run_id=uuid5(NAMESPACE_URL, f"agent-run:{state['handoff'].idempotency_key}"),
            tool_id=plan.tool_id,
            tool_version=tool_manifest.version,
            call_id=f"{state['handoff'].step_id}:manage:1",
            actor=state["handoff"].actor,
            typed_input=trusted_input,
            idempotency_key=f"{state['handoff'].idempotency_key}:{plan.tool_id}",
        )
        try:
            result = await self._tool_executor.execute(request)
        except Exception:
            return self._failure("PLANNING_TOOL_FAILED")
        if result.status != "SUCCEEDED":
            return self._failure(
                result.safe_error_code or "PLANNING_TOOL_FAILED", preserve_code=True
            )
        try:
            output = PlanningAgentOutput.model_validate(result.typed_output)
        except ValidationError:
            return self._failure("PLANNING_TOOL_OUTPUT_INVALID")
        return {
            "tool_result": result,
            "output": output,
            "tool_calls_used": state["tool_calls_used"] + 1,
            "route": "execute",
        }

    async def verify_result(self, state: PlanningAgentState) -> dict[str, object]:
        value = state["value"]
        output = state["output"]
        if value is None or output is None:
            return self._failure("PLANNING_RESULT_INVALID")
        try:
            verify_planning_result(value, output)
        except PlanningResultError as error:
            return self._failure(str(error))
        return {"route": "execute", "stop_reason": "COMPLETED"}

    async def manual_editable_fallback(self, state: PlanningAgentState) -> dict[str, object]:
        return {
            "route": "manual_fallback",
            "safe_error_code": state["safe_error_code"] or _MANUAL_FALLBACK_CODE,
        }

    async def return_agent_result(self, state: PlanningAgentState) -> dict[str, object]:
        plan = state["plan"]
        requested = plan.requested_handoff if plan is not None else None
        output = state["output"]
        if state["route"] == "manual_fallback":
            status = AgentRunStatus.FAILED
        elif requested is not None:
            status = AgentRunStatus.COMPLETED
        elif output is not None and output.awaiting == "MANAGER_INPUT":
            status = AgentRunStatus.AWAITING_INPUT
        elif output is not None and output.awaiting == "MANAGER_DECISION":
            status = AgentRunStatus.AWAITING_HUMAN
        else:
            status = AgentRunStatus.COMPLETED

        proposed_actions: tuple[ProposedAction, ...] = ()
        if output is not None and output.awaiting == "MANAGER_DECISION":
            proposed_actions = (
                ProposedAction(
                    action_type="planning.proposal_review",
                    risk=RiskLevel.PROPOSAL_ONLY,
                    requires_human_gate=True,
                    reference_id=output.proposal_id,
                ),
            )
        typed_output = (
            output.model_dump(mode="json")
            if output is not None
            else cast(dict[str, JsonValue], {"fallback": "manual_editable"})
        )
        passed = (
            status
            in {
                AgentRunStatus.COMPLETED,
                AgentRunStatus.AWAITING_INPUT,
                AgentRunStatus.AWAITING_HUMAN,
            }
            and requested is None
        )
        verifier_results = tuple(
            VerifierResult(
                verifier_id=reference.split("@", maxsplit=1)[0],
                verifier_version="1.0.0",
                passed=passed,
                safe_codes=(
                    () if state["safe_error_code"] is None else (state["safe_error_code"],)
                ),
            )
            for reference in self._manifest.evaluators
        )
        evidence = state["tool_result"].evidence if state["tool_result"] is not None else ()
        return {
            "result": AgentResult(
                interaction_outcome=(
                    InteractionOutcome.ACTION_REVIEW_REQUIRED
                    if output is not None
                    and output.operation is PlanningOperation.PREPARE_ACTION
                    and output.proposal_id is not None
                    and output.approval_id is not None
                    else None
                ),
                agent_id=AgentId.PLANNING,
                agent_version=self._manifest.agent.version,
                status=status,
                typed_output=typed_output,
                evidence=evidence,
                proposed_actions=proposed_actions,
                verifier_results=verifier_results,
                requested_handoff=requested,
                iterations_used=state["iterations_used"],
                tool_calls_used=state["tool_calls_used"],
                stop_reason=state["stop_reason"],
                safe_error_code=state["safe_error_code"],
            )
        }

    @staticmethod
    def _failure(code: str, *, preserve_code: bool = False) -> dict[str, object]:
        return {
            "route": "manual_fallback",
            "stop_reason": code,
            "safe_error_code": code if preserve_code else _MANUAL_FALLBACK_CODE,
        }
