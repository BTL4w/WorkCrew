"""Guarded read-only Work Intelligence Agent Harness."""

from datetime import UTC, datetime
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ValidationError

from work_management_ai.agents.work_intelligence.contracts import (
    EvidenceItem,
    GroundedAnswerDraft,
    ReadToolEnvelope,
    WorkIntelligenceInput,
    WorkIntelligenceOutput,
    WorkQuestionKind,
    WorkStepPlan,
)
from work_management_ai.agents.work_intelligence.evaluators.grounding import (
    GroundingError,
    verify_grounded_answer,
)
from work_management_ai.agents.work_intelligence.prompts import (
    build_answer_messages,
    build_step_plan_messages,
)
from work_management_ai.agents.work_intelligence.skills import AnswerWorkQuestionSkillLoader
from work_management_ai.agents.work_intelligence.workflows.graph import (
    WorkIntelligenceGraph,
    WorkIntelligenceState,
)
from work_management_ai.model_gateway.contracts import ModelGateway, StructuredModelRequest
from work_management_ai.model_gateway.errors import ModelGatewayError
from work_management_ai.runtime.contracts import (
    AgentHandoff,
    AgentId,
    AgentResult,
    AgentRunStatus,
    ContextReference,
    JsonValue,
    ToolExecutionRequest,
    ToolExecutorPort,
    VerifierResult,
)
from work_management_ai.runtime.manifests import (
    AgentManifest,
    ToolManifest,
    load_yaml_resource,
    resolve_contract,
)

_AGENT_PACKAGE = "work_management_ai.agents.work_intelligence"
_TOOL_PACKAGES = {
    "risk.read": "work_management_ai.tools.risk",
    "work.read_my_tasks": "work_management_ai.tools.work.read_my_tasks",
    "work.read_resource": "work_management_ai.tools.work.read_resource",
}


class WorkIntelligenceHarness:
    def __init__(self, *, model_gateway: ModelGateway, tool_executor: ToolExecutorPort) -> None:
        self._model_gateway = model_gateway
        self._tool_executor = tool_executor
        self._manifest = load_yaml_resource(_AGENT_PACKAGE, "agent.yaml", AgentManifest)
        self._skill_loader = AnswerWorkQuestionSkillLoader()
        self._graph = WorkIntelligenceGraph(self)

    async def run(self, handoff: AgentHandoff) -> AgentResult:
        return await self._graph.run(
            WorkIntelligenceState(
                handoff=handoff,
                value=None,
                plan=None,
                tool_result=None,
                evidence=(),
                draft=None,
                output=None,
                result=None,
                skill_instructions="",
                route="execute",
                stop_reason="NOT_STARTED",
                safe_error_code=None,
                iterations_used=0,
                tool_calls_used=0,
                model_refs=(),
            )
        )

    async def receive_handoff(self, state: WorkIntelligenceState) -> dict[str, object]:
        return {"stop_reason": "RUNNING"}

    async def validate_contract(self, state: WorkIntelligenceState) -> dict[str, object]:
        handoff = state["handoff"]
        runtime = self._manifest.runtime
        if (
            handoff.target_agent_id is not AgentId.WORK_INTELLIGENCE
            or handoff.target_agent_version != self._manifest.agent.version
            or handoff.capability not in self._manifest.capabilities
        ):
            return self._failure("WORK_HANDOFF_INVALID")
        if (
            handoff.budget.max_iterations > runtime.max_iterations
            or handoff.budget.max_tool_calls > runtime.max_tool_calls
            or handoff.budget.max_handoffs > runtime.max_handoffs
            or handoff.budget.max_replans > runtime.max_replans
            or handoff.budget.timeout_seconds > runtime.timeout_seconds
        ):
            return self._failure("WORK_BUDGET_INVALID")
        try:
            value = WorkIntelligenceInput.model_validate(handoff.typed_input)
        except ValidationError:
            return self._failure("WORK_INPUT_INVALID")
        return {"value": value, "route": "execute"}

    async def build_specialist_context(self, state: WorkIntelligenceState) -> dict[str, object]:
        if any(
            reference.organization_id != state["handoff"].actor.organization_id
            for reference in state["handoff"].context_references
        ):
            return self._failure("WORK_CONTEXT_TENANT_MISMATCH")
        return {"route": "execute"}

    async def create_step_plan(self, state: WorkIntelligenceState) -> dict[str, object]:
        value = state["value"]
        if value is None:
            return self._failure("WORK_INPUT_INVALID")
        if state["iterations_used"] >= state["handoff"].budget.max_iterations:
            return self._failure("WORK_ITERATION_BUDGET_EXHAUSTED")
        request = StructuredModelRequest(
            invocation_key=f"work_intelligence.{value.locale}.plan",
            messages=build_step_plan_messages(
                value,
                skill_catalog=self._skill_loader.catalog(),
                context_references=state["handoff"].context_references,
            ),
            output_schema=WorkStepPlan,
            timeout_seconds=60,
        )
        try:
            response = await self._model_gateway.generate_structured(request)
        except ModelGatewayError:
            return self._failure("WORK_MODEL_PLAN_FAILED")
        route = "requested_handoff" if response.parsed.requested_handoff is not None else "execute"
        return {
            "plan": response.parsed,
            "iterations_used": state["iterations_used"] + 1,
            "model_refs": (*state["model_refs"], response.model_ref),
            "route": route,
            "stop_reason": "REQUESTED_HANDOFF" if route == "requested_handoff" else "RUNNING",
        }

    async def load_answer_work_question_skill(
        self, state: WorkIntelligenceState
    ) -> dict[str, object]:
        plan = state["plan"]
        if plan is None:
            return self._failure("WORK_STEP_PLAN_MISSING")
        try:
            skill, instructions = self._skill_loader.load(plan.skill_reference)
        except (ValueError, ValidationError):
            return self._failure("WORK_SKILL_INVALID")
        skill_reference = skill.name + "@" + skill.version.split(".", maxsplit=1)[0]
        if skill_reference not in self._manifest.allowed_skills:
            return self._failure("WORK_SKILL_NOT_ALLOWED")
        return {"skill_instructions": instructions, "route": "execute"}

    async def execute_allowed_read_tool(self, state: WorkIntelligenceState) -> dict[str, object]:
        plan = state["plan"]
        if plan is None or plan.tool_id is None:
            return self._failure("WORK_TOOL_PLAN_INVALID")
        if state["tool_calls_used"] >= state["handoff"].budget.max_tool_calls:
            return self._failure("WORK_TOOL_BUDGET_EXHAUSTED")
        tool_reference = f"{plan.tool_id}@1"
        if tool_reference not in self._manifest.allowed_tools:
            return self._failure("WORK_TOOL_NOT_ALLOWED")
        package = _TOOL_PACKAGES.get(plan.tool_id)
        if package is None:
            return self._failure("WORK_TOOL_NOT_ALLOWED")
        tool_manifest = load_yaml_resource(package, "tool.yaml", ToolManifest)
        skill, _ = self._skill_loader.load(plan.skill_reference)
        if (
            tool_reference not in skill.allowed_tools
            or tool_manifest.risk_level.value != "READ_ONLY"
        ):
            return self._failure("WORK_TOOL_NOT_ALLOWED")
        try:
            input_contract = cast(type[BaseModel], resolve_contract(tool_manifest.input_contract))
            validated_input = input_contract.model_validate(plan.tool_input).model_dump(mode="json")
        except (AttributeError, TypeError, ValidationError, ValueError):
            return self._failure("WORK_TOOL_INPUT_INVALID")
        request = ToolExecutionRequest(
            agent_run_id=uuid5(NAMESPACE_URL, f"agent-run:{state['handoff'].idempotency_key}"),
            tool_id=plan.tool_id,
            tool_version=tool_manifest.version,
            call_id=f"{state['handoff'].step_id}:read:1",
            actor=state["handoff"].actor,
            typed_input=cast(dict[str, JsonValue], validated_input),
            idempotency_key=f"{state['handoff'].idempotency_key}:{plan.tool_id}",
        )
        try:
            result = await self._tool_executor.execute(request)
        except Exception:
            return self._failure("WORK_TOOL_FAILED")
        if result.status != "SUCCEEDED":
            return self._failure("WORK_TOOL_FAILED")
        try:
            if plan.tool_id == "risk.read":
                from work_management_ai.agents.risk.contracts import RiskExplanationInput

                context = RiskExplanationInput.model_validate(result.typed_output)
                envelope = ReadToolEnvelope(
                    resolution="UNIQUE",
                    evidence=(
                        EvidenceItem(
                            evidence_id=f"RISK_CONTEXT:{context.task_id}:{context.fingerprint}",
                            resource_type="RISK_CONTEXT",
                            resource_id=context.task_id,
                            resource_version=context.task_version,
                            observed_at=datetime.now(UTC),
                            fields={
                                "score": context.score,
                                "state": context.state,
                                "scope": context.scope,
                                "rationale": context.rationale,
                                "limitations": list(context.limitations),
                                "facts": [
                                    f.model_dump(mode="json") for f in context.permitted_sources
                                ],
                            },
                        ),
                    ),
                )
            else:
                envelope = ReadToolEnvelope.model_validate(result.typed_output)
        except ValidationError:
            return self._failure("WORK_TOOL_OUTPUT_INVALID")
        evidence = envelope.evidence
        if plan.question_kind is WorkQuestionKind.NEXT_TASK and envelope.next_task_id is not None:
            evidence = tuple(item for item in evidence if item.resource_id == envelope.next_task_id)
        updates: dict[str, object] = {
            "tool_result": result,
            "evidence": evidence,
            "tool_calls_used": state["tool_calls_used"] + 1,
        }
        if envelope.resolution == "AMBIGUOUS":
            updates.update(
                output=self._clarification_output(plan.question_kind, state, ambiguous=True),
                route="awaiting_input",
                stop_reason="WORK_RESOURCE_AMBIGUOUS",
            )
        elif envelope.resolution == "NOT_FOUND":
            updates.update(
                output=self._clarification_output(plan.question_kind, state, ambiguous=False),
                route="not_found",
                stop_reason="WORK_RESOURCE_NOT_FOUND",
                safe_error_code="WORK_RESOURCE_NOT_FOUND",
            )
        else:
            updates["route"] = "execute"
        return updates

    async def synthesize_grounded_answer(self, state: WorkIntelligenceState) -> dict[str, object]:
        value = state["value"]
        plan = state["plan"]
        if value is None or plan is None:
            return self._failure("WORK_STATE_INVALID")
        if state["iterations_used"] >= state["handoff"].budget.max_iterations:
            return self._failure("WORK_ITERATION_BUDGET_EXHAUSTED")
        request = StructuredModelRequest(
            invocation_key=f"work_intelligence.{value.locale}.synthesize",
            messages=build_answer_messages(
                value,
                question_kind=plan.question_kind,
                evidence=state["evidence"],
                skill_instructions=state["skill_instructions"],
            ),
            output_schema=GroundedAnswerDraft,
            timeout_seconds=60,
        )
        try:
            response = await self._model_gateway.generate_structured(request)
        except ModelGatewayError:
            return self._failure("WORK_MODEL_SYNTHESIS_FAILED")
        output = WorkIntelligenceOutput(
            question_kind=response.parsed.question_kind,
            claims=response.parsed.claims,
            evidence=state["evidence"],
            needs_clarification=response.parsed.needs_clarification,
            clarification_question=response.parsed.clarification_question,
        )
        return {
            "draft": response.parsed,
            "output": output,
            "iterations_used": state["iterations_used"] + 1,
            "model_refs": (*state["model_refs"], response.model_ref),
            "route": "execute",
        }

    async def verify_result(self, state: WorkIntelligenceState) -> dict[str, object]:
        output = state["output"]
        plan = state["plan"]
        if output is None or plan is None or output.question_kind is not plan.question_kind:
            return self._failure("WORK_GROUNDING_FAILED")
        try:
            verify_grounded_answer(output)
        except GroundingError:
            return self._failure("WORK_GROUNDING_FAILED")
        if plan.tool_id == "risk.read":
            if state["tool_calls_used"] >= state["handoff"].budget.max_tool_calls:
                return self._failure("WORK_TOOL_BUDGET_EXHAUSTED")
            context = state["evidence"][0]
            recheck = await self._tool_executor.execute(
                ToolExecutionRequest(
                    agent_run_id=uuid5(
                        NAMESPACE_URL, f"agent-run:{state['handoff'].idempotency_key}"
                    ),
                    tool_id="risk.read",
                    tool_version="1.0.0",
                    call_id=f"{state['handoff'].step_id}:revalidate",
                    actor=state["handoff"].actor,
                    typed_input={
                        "task_reference": str(context.resource_id),
                        "expected_fingerprint": context.evidence_id.rsplit(":", 1)[1],
                    },
                    idempotency_key=f"{state['handoff'].idempotency_key}:revalidate",
                )
            )
            state["tool_calls_used"] += 1
            if recheck.status != "SUCCEEDED":
                return self._failure("WORK_CONTEXT_CHANGED")
        return {"route": "execute", "stop_reason": "COMPLETED"}

    async def manual_read_fallback(self, state: WorkIntelligenceState) -> dict[str, object]:
        value = state["value"]
        locale = value.locale if value is not None else "en"
        kind = (
            value.requested_kind
            if value is not None and value.requested_kind is not None
            else WorkQuestionKind.PROJECT_DETAIL
        )
        question = (
            "Vui lòng dùng màn hình Công việc để xem dữ liệu được phép."
            if locale == "vi"
            else "Please use the Work view to inspect permitted data."
        )
        return {
            "output": WorkIntelligenceOutput(
                question_kind=kind,
                claims=(),
                evidence=(),
                needs_clarification=True,
                clarification_question=question,
            ),
            "route": "manual_fallback",
            "safe_error_code": "WORK_MANUAL_READ_FALLBACK",
        }

    async def return_agent_result(self, state: WorkIntelligenceState) -> dict[str, object]:
        plan = state["plan"]
        requested = plan.requested_handoff if plan is not None else None
        output = state["output"]
        if output is None:
            kind = plan.question_kind if plan is not None else WorkQuestionKind.PROJECT_DETAIL
            output = WorkIntelligenceOutput(
                question_kind=kind,
                claims=(),
                evidence=(),
                needs_clarification=False,
                clarification_question=None,
            )
        if state["route"] == "manual_fallback":
            status = AgentRunStatus.FAILED
        elif state["route"] == "awaiting_input":
            status = AgentRunStatus.AWAITING_INPUT
        else:
            status = AgentRunStatus.COMPLETED
        evidence = tuple(_context_reference(item, state["handoff"]) for item in output.evidence)
        verifier_results = (
            VerifierResult(
                verifier_id="work_grounding",
                verifier_version="1.0.0",
                passed=status is AgentRunStatus.COMPLETED and requested is None,
                safe_codes=(
                    () if state["safe_error_code"] is None else (state["safe_error_code"],)
                ),
            ),
        )
        return {
            "result": AgentResult(
                agent_id=AgentId.WORK_INTELLIGENCE,
                agent_version=self._manifest.agent.version,
                status=status,
                typed_output=output.model_dump(mode="json"),
                evidence=evidence,
                verifier_results=verifier_results,
                requested_handoff=requested,
                iterations_used=state["iterations_used"],
                tool_calls_used=state["tool_calls_used"],
                stop_reason=state["stop_reason"],
                safe_error_code=state["safe_error_code"],
            )
        }

    def _clarification_output(
        self,
        kind: WorkQuestionKind,
        state: WorkIntelligenceState,
        *,
        ambiguous: bool,
    ) -> WorkIntelligenceOutput:
        value = state["value"]
        locale = value.locale if value is not None else "en"
        if ambiguous:
            question = (
                "Bạn muốn nói đến tài nguyên nào?"
                if locale == "vi"
                else "Which permitted resource did you mean?"
            )
        else:
            question = (
                "Không tìm thấy tài nguyên được phép xem."
                if locale == "vi"
                else "The requested resource was not found."
            )
        return WorkIntelligenceOutput(
            question_kind=kind,
            claims=(),
            evidence=(),
            needs_clarification=True,
            clarification_question=question,
        )

    @staticmethod
    def _failure(code: str) -> dict[str, object]:
        return {
            "route": "manual_fallback",
            "stop_reason": code,
            "safe_error_code": "WORK_MANUAL_READ_FALLBACK",
        }


def _context_reference(item: object, handoff: AgentHandoff) -> ContextReference:
    from work_management_ai.agents.work_intelligence.contracts import EvidenceItem

    evidence = EvidenceItem.model_validate(item)
    return ContextReference(
        reference_id=uuid5(NAMESPACE_URL, evidence.evidence_id),
        organization_id=handoff.actor.organization_id,
        resource_type=evidence.resource_type,
        resource_id=evidence.resource_id,
        version=evidence.resource_version,
        observed_at=evidence.observed_at,
    )
