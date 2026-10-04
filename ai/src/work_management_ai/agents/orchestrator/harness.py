"""Guarded runtime for bounded Orchestrator planning and delegation."""

import asyncio
import re
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from work_management_ai.agents.orchestrator.contracts import (
    ActorContextResolverPort,
    ExecutionPlan,
    ExecutionStep,
    OrchestratorInput,
    OrchestratorOutput,
    OrchestratorStatus,
    OrchestratorSynthesis,
    PendingFollowup,
    ScheduleIntent,
    SpecialistRunnerPort,
    StepMode,
)
from work_management_ai.agents.orchestrator.evaluators.plan import (
    ExecutionPlanError,
    ready_batches,
    validate_execution_plan,
    validate_replan,
)
from work_management_ai.agents.orchestrator.prompts import (
    build_plan_messages,
    build_synthesis_messages,
)
from work_management_ai.agents.orchestrator.prompts.schedule_v1 import build_schedule_messages
from work_management_ai.agents.orchestrator.workflows.graph import (
    OrchestratorGraph,
    OrchestratorState,
)
from work_management_ai.agents.risk.contracts import RiskCardContent, RiskReplanRequest
from work_management_ai.model_gateway.contracts import (
    ModelGateway,
    StructuredModelRequest,
)
from work_management_ai.model_gateway.errors import ModelGatewayError
from work_management_ai.runtime.agent_registry import AgentRegistry
from work_management_ai.runtime.contracts import (
    ActivityResponseBlock,
    AgentBudget,
    AgentHandoff,
    AgentId,
    AgentResult,
    AgentRunStatus,
    AssignmentResultResponseBlock,
    CapabilityUnavailableResponseBlock,
    DailySummaryResponseBlock,
    DailyUpdateResponseBlock,
    JsonValue,
    PlanningRunResponseBlock,
    PublicEvidenceReference,
    QuestionResponseBlock,
    ResponseBlock,
    RiskResponseBlock,
    SafeErrorResponseBlock,
    TeamRecommendationResponseBlock,
    TextResponseBlock,
    ToolExecutionRequest,
    ToolExecutorPort,
    WorkEvidenceResponseBlock,
)
from work_management_ai.runtime.policy_guard import AgentPolicyError, PolicyGuard

_MAX_PLAN_REPAIRS = 1
_MAX_REPLANS = 2
_MAX_HANDOFFS = 6
_ACTIVE_PHASE = 4
_REVISION_SIGNALS = (
    "add",
    "change",
    "chỉnh",
    "cập nhật",
    "delete",
    "dời",
    "edit",
    "extend",
    "kéo dài",
    "mở rộng",
    "move",
    "remove",
    "revise",
    "replan",
    "rút ngắn",
    "sửa",
    "thay đổi",
    "thêm",
    "update",
    "xóa",
)


class OrchestratorHarness:
    def __init__(
        self,
        *,
        model_gateway: ModelGateway,
        registry: AgentRegistry,
        policy_guard: PolicyGuard,
        actor_resolver: ActorContextResolverPort,
        specialists: SpecialistRunnerPort,
        automation_tools: ToolExecutorPort | None = None,
    ) -> None:
        self._model_gateway = model_gateway
        self._registry = registry
        self._guard = policy_guard
        self._actor_resolver = actor_resolver
        self._specialists = specialists
        self._automation_tools = automation_tools
        self._graph = OrchestratorGraph(self)

    async def run_turn(self, value: OrchestratorInput) -> OrchestratorOutput:
        return await self._graph.run(
            OrchestratorState(
                value=value,
                current_actor=None,
                plan=None,
                prior_plan=None,
                original_plan=None,
                results=(),
                completed_step_ids=(),
                last_batch_handoffs=(),
                last_batch_results=(),
                blocks=(),
                status=OrchestratorStatus.FAILED,
                stop_reason="NOT_STARTED",
                route="execute",
                repair_attempts=0,
                replans_used=0,
                handoffs_used=0,
                model_refs=(),
                pending_requested_handoff=None,
                risk_replan=None,
                output=None,
            )
        )

    async def intake_turn(self, state: OrchestratorState) -> dict[str, object]:
        try:
            actor = await self._actor_resolver.resolve(state["value"].actor)
        except Exception:
            return self._failure("ACTOR_CONTEXT_UNAVAILABLE")
        if not actor.is_active:
            return self._failure("ACTOR_INACTIVE")
        if (
            actor.membership_id != state["value"].actor.membership_id
            or actor.organization_id != state["value"].actor.organization_id
        ):
            return self._failure("ACTOR_CONTEXT_MISMATCH")
        return {
            "current_actor": actor,
            "route": "execute",
            "stop_reason": "RUNNING",
        }

    async def build_context(self, state: OrchestratorState) -> dict[str, object]:
        message = state["value"].message.casefold()
        if self._automation_tools and any(
            signal in message
            for signal in (
                "daily summary",
                "daily summaries",
                "tổng hợp hằng ngày",
                "tổng hợp hàng ngày",
            )
        ):
            return {"route": "schedule"}
        if state["value"].active_context.daily_update_resolution_issue:
            return {"route": "ask_user", "stop_reason": "DAILY_UPDATE_CLARIFICATION_REQUIRED"}
        if state["value"].active_context.assignment_resolution_issue is not None:
            return {"route": "ask_user", "stop_reason": "ASSIGNMENT_CLARIFICATION_REQUIRED"}
        return {"route": "execute"}

    async def prepare_schedule(self, state: OrchestratorState) -> dict[str, object]:
        actor, value = state["current_actor"], state["value"]
        if actor is None or actor.role not in {"MANAGER", "ADMIN"}:
            return self._failure("SCHEDULE_FORBIDDEN")
        manifest = self._registry.resolve(AgentId.ORCHESTRATOR, "1.0.0", active_phase=4).manifest
        if (
            "automation.preview@1" not in manifest.allowed_tools
            or manifest.runtime.max_tool_calls < 1
        ):
            return self._failure("TOOL_NOT_ALLOWED")
        assert self._automation_tools is not None
        try:
            response = await self._model_gateway.generate_structured(
                StructuredModelRequest(
                    invocation_key=f"orchestrator.schedule.{value.locale}.v1",
                    messages=build_schedule_messages(value.message),
                    output_schema=ScheduleIntent,
                    timeout_seconds=30,
                )
            )
            request = ToolExecutionRequest(
                tool_id="automation.preview",
                tool_version="1.0.0",
                call_id=f"schedule:{value.turn_id}",
                idempotency_key=f"schedule:{value.turn_id}",
                agent_run_id=uuid5(NAMESPACE_URL, f"orchestrator:{value.turn_id}"),
                actor=value.actor,
                typed_input=cast(dict[str, JsonValue], response.parsed.model_dump(mode="json")),
            )
            result = await asyncio.wait_for(self._automation_tools.execute(request), timeout=15)
            if result.status != "SUCCEEDED":
                return self._failure("SCHEDULE_PREVIEW_UNAVAILABLE")
            block = DailySummaryResponseBlock.model_validate(result.typed_output)
            return {
                "plan": ExecutionPlan(
                    objectives=(value.message,),
                    response_language=value.locale,
                    schedule_intent=response.parsed,
                ),
                "blocks": (block,),
                "status": OrchestratorStatus.AWAITING_HUMAN,
                "stop_reason": "SCHEDULE_CONFIRMATION_REQUIRED",
                "model_refs": (*state["model_refs"], response.model_ref),
                "route": "execute",
            }
        except (ModelGatewayError, ValueError, TimeoutError):
            return self._failure("SCHEDULE_PREVIEW_UNAVAILABLE")

    async def plan_objective(self, state: OrchestratorState) -> dict[str, object]:
        trusted_plan = self._trusted_action_plan(state)
        if trusted_plan is not None:
            updates: dict[str, object] = {
                "plan": trusted_plan,
                "stop_reason": "RUNNING",
            }
            if state["original_plan"] is None:
                updates["original_plan"] = trusted_plan
            return updates
        requested = state["pending_requested_handoff"]
        if requested is not None:
            mode = f"replan.{state['replans_used']}"
        elif state["repair_attempts"]:
            mode = "repair"
        else:
            mode = "plan"
        request = StructuredModelRequest(
            invocation_key=f"orchestrator.{state['value'].locale}.{mode}",
            messages=build_plan_messages(
                state["value"],
                mode=mode,
                requested_handoff=requested,
                prior_plan=state["prior_plan"],
                specialist_catalog=self._registry.planning_catalog(
                    active_phase=_ACTIVE_PHASE,
                    role=(state["current_actor"].role if state["current_actor"] else "EMPLOYEE"),
                ),
            ),
            output_schema=ExecutionPlan,
            timeout_seconds=60,
        )
        try:
            response = await self._model_gateway.generate_structured(request)
        except ModelGatewayError:
            return {"plan": None, "stop_reason": "MODEL_PLAN_INVALID"}
        updates: dict[str, object] = {
            "plan": response.parsed,
            "model_refs": (*state["model_refs"], response.model_ref),
            "stop_reason": "RUNNING",
        }
        if state["original_plan"] is None:
            updates["original_plan"] = response.parsed
        return updates

    def _trusted_action_plan(self, state: OrchestratorState) -> ExecutionPlan | None:
        daily = state["value"].active_context.daily_update
        if daily is not None:
            return ExecutionPlan(
                objectives=("Prepare owner daily update",),
                response_language=daily.locale,
                steps=(
                    ExecutionStep(
                        step_id="daily-update",
                        target_agent_id=AgentId.DAILY_UPDATE,
                        target_agent_version="1.0.0",
                        capability="daily_update.prepare",
                        objective="Prepare an owner-reviewed draft; do not confirm",
                        typed_input={},
                        mode=StepMode.PROPOSAL,
                    ),
                ),
            )
        assignment = self._trusted_assignment_action_plan(state)
        if assignment is not None:
            return assignment
        return self._trusted_planning_action_plan(state)

    def _trusted_planning_action_plan(self, state: OrchestratorState) -> ExecutionPlan | None:
        active = state["value"].active_context.active_planning
        if active is None or active.requested_operation is None:
            return None
        capability = {
            "RESUME_INPUT": "planning.resume",
            "REVISE": "planning.revise",
        }[active.requested_operation]
        actor = state["current_actor"]
        catalog = self._registry.planning_catalog(
            active_phase=_ACTIVE_PHASE,
            role=(actor.role if actor is not None else "EMPLOYEE"),
        )
        planning_agent = next(
            (
                item
                for item in catalog
                if item["agent_id"] == AgentId.PLANNING.value
                and capability in cast(list[object], item["capabilities"])
            ),
            None,
        )
        if planning_agent is None:
            return ExecutionPlan(
                objectives=(state["value"].message,),
                unavailable_capabilities=(capability,),
                response_language=state["value"].locale,
            )
        return ExecutionPlan(
            objectives=(state["value"].message,),
            steps=(
                ExecutionStep(
                    step_id=(
                        "resume_planning"
                        if active.requested_operation == "RESUME_INPUT"
                        else "revise_plan"
                    ),
                    target_agent_id=AgentId.PLANNING,
                    target_agent_version=str(planning_agent["agent_version"]),
                    capability=capability,
                    objective=state["value"].message,
                    typed_input={},
                    mode=StepMode.PROPOSAL,
                ),
            ),
            response_language=state["value"].locale,
        )

    def _trusted_assignment_action_plan(self, state: OrchestratorState) -> ExecutionPlan | None:
        value = state["value"]
        active_team = value.active_context.active_team
        exact = value.active_context.exact_assignment
        capability: str | None = None
        step_id = "assignment"
        mode = StepMode.PROPOSAL
        if active_team is not None and active_team.requested_operation == "REVISE_TEAM":
            capability = "assignment.revise_team"
            step_id = "revise_team"
        elif active_team is not None and active_team.requested_operation == "RECOMMEND_TEAM":
            capability = "assignment.recommend_team"
            step_id = "recommend_team"
        elif active_team is not None and active_team.requested_operation == "ANALYZE_WORKLOAD":
            capability = "assignment.analyze_workload"
            step_id = "analyze_workload"
        elif exact is not None:
            capability = "assignment.assign_task_explicitly"
            step_id = "assign_task"
            mode = StepMode.EXPLICIT_WRITE
        if capability is None:
            return None
        actor = state["current_actor"]
        catalog = self._registry.planning_catalog(
            active_phase=_ACTIVE_PHASE,
            role=(actor.role if actor is not None else "EMPLOYEE"),
        )
        registered = next(
            (
                item
                for item in catalog
                if item["agent_id"] == AgentId.ASSIGNMENT.value
                and capability in cast(list[object], item["capabilities"])
            ),
            None,
        )
        if registered is None:
            return ExecutionPlan(
                objectives=(value.message,),
                unavailable_capabilities=(capability,),
                response_language=value.locale,
            )
        return ExecutionPlan(
            objectives=(value.message,),
            steps=(
                ExecutionStep(
                    step_id=step_id,
                    target_agent_id=AgentId.ASSIGNMENT,
                    target_agent_version=str(registered["agent_version"]),
                    capability=capability,
                    objective=value.message,
                    typed_input={},
                    mode=mode,
                ),
            ),
            response_language=value.locale,
        )

    async def validate_plan(self, state: OrchestratorState) -> dict[str, object]:
        plan = state["plan"]
        if plan is None:
            if (
                state["repair_attempts"] < _MAX_PLAN_REPAIRS
                and state["pending_requested_handoff"] is None
            ):
                return {"route": "repair"}
            return {"route": "manual_fallback", "stop_reason": "MODEL_PLAN_INVALID"}
        actor = state["current_actor"]
        if actor is None:
            return self._failure("ACTOR_CONTEXT_UNAVAILABLE")
        try:
            validate_execution_plan(plan, self._registry, actor)
            if plan.response_language != state["value"].locale:
                raise ExecutionPlanError("RESPONSE_LANGUAGE_MISMATCH")
            for step in plan.steps:
                if step.target_agent_id is AgentId.DAILY_UPDATE and (
                    state["value"].active_context.daily_update is None
                    or step.mode is not StepMode.PROPOSAL
                ):
                    raise ExecutionPlanError("DAILY_UPDATE_TRUSTED_CONTEXT_REQUIRED")
            self._validate_planning_context(plan, state["value"])
            self._validate_assignment_context(plan, state["value"])
            requested = state["pending_requested_handoff"]
            prior = state["prior_plan"]
            if requested is not None and prior is not None:
                validate_replan(
                    prior=prior,
                    candidate=plan,
                    completed_step_ids=frozenset(state["completed_step_ids"]),
                    requested_handoff=requested,
                )
        except ExecutionPlanError:
            if (
                state["repair_attempts"] < _MAX_PLAN_REPAIRS
                and state["pending_requested_handoff"] is None
            ):
                return {"route": "repair", "stop_reason": "EXECUTION_PLAN_INVALID"}
            return {"route": "manual_fallback", "stop_reason": "EXECUTION_PLAN_INVALID"}
        if not plan.steps and plan.unavailable_capabilities:
            return {"route": "capability_unavailable"}
        return {"route": "execute", "pending_requested_handoff": None}

    @staticmethod
    def _validate_planning_context(plan: ExecutionPlan, value: OrchestratorInput) -> None:
        active = value.active_context.active_planning
        for step in plan.steps:
            if step.capability == "planning.revise":
                if active is None or active.proposal_id is None or active.proposal_version is None:
                    raise ExecutionPlanError("PLANNING_REVISION_CONTEXT_MISSING")
                if active.requested_operation != "REVISE" and not _has_revision_signal(
                    value.message
                ):
                    raise ExecutionPlanError("PLANNING_REVISION_INTENT_MISSING")
            elif step.capability == "planning.resume" and (
                active is None or active.requested_operation != "RESUME_INPUT"
            ):
                raise ExecutionPlanError("PLANNING_RESUME_CONTEXT_MISSING")

    @staticmethod
    def _validate_assignment_context(plan: ExecutionPlan, value: OrchestratorInput) -> None:
        assignment_steps = tuple(
            step for step in plan.steps if step.target_agent_id is AgentId.ASSIGNMENT
        )
        if not assignment_steps:
            return
        active = value.active_context
        if active.exact_assignment is not None and all(
            step.capability == "assignment.assign_task_explicitly" for step in assignment_steps
        ):
            return
        if active.active_team is not None and active.active_team.requested_operation is not None:
            expected = {
                "REVISE_TEAM": "assignment.revise_team",
                "RECOMMEND_TEAM": "assignment.recommend_team",
                "ANALYZE_WORKLOAD": "assignment.analyze_workload",
            }[active.active_team.requested_operation]
            if all(step.capability == expected for step in assignment_steps):
                return
        planning_steps = {
            step.step_id for step in plan.steps if step.capability == "planning.create"
        }
        combined_is_explicit = (
            _has_combined_project_team_signal(value.message)
            and bool(planning_steps)
            and all(
                step.capability == "assignment.recommend_team"
                and bool(planning_steps.intersection(step.depends_on))
                for step in assignment_steps
            )
        )
        if not combined_is_explicit:
            raise ExecutionPlanError("ASSIGNMENT_TRUSTED_INTENT_MISSING")

    async def select_next_step(self, state: OrchestratorState) -> dict[str, object]:
        plan = state["plan"]
        if plan is None:
            return self._failure("EXECUTION_PLAN_MISSING")
        if len(state["completed_step_ids"]) == len(plan.steps):
            return {"route": "synthesize"}
        batches = ready_batches(plan, frozenset(state["completed_step_ids"]))
        if not batches:
            return self._failure("NO_READY_EXECUTION_STEP")
        return {"route": "delegate"}

    async def delegate_specialist(self, state: OrchestratorState) -> dict[str, object]:
        plan = state["plan"]
        actor = state["current_actor"]
        if plan is None or actor is None:
            return self._failure("ORCHESTRATOR_STATE_INVALID")
        batch = ready_batches(plan, frozenset(state["completed_step_ids"]))[0]
        if state["handoffs_used"] + len(batch) > _MAX_HANDOFFS:
            return self._failure("HANDOFF_BUDGET_EXHAUSTED")
        handoffs: list[AgentHandoff] = []
        for step in batch:
            registered = self._registry.resolve(
                step.target_agent_id,
                step.target_agent_version,
                active_phase=_ACTIVE_PHASE,
            )
            runtime = registered.manifest.runtime
            handoff = AgentHandoff(
                orchestration_run_id=(
                    state["value"].orchestration_run_id
                    or uuid5(NAMESPACE_URL, f"orchestration:{state['value'].turn_id}")
                ),
                parent_agent_run_id=uuid5(NAMESPACE_URL, f"orchestrator:{state['value'].turn_id}"),
                target_agent_id=step.target_agent_id,
                target_agent_version=step.target_agent_version,
                capability=step.capability,
                objective=step.objective,
                typed_input=self._trusted_specialist_input(
                    step, state["value"], state["risk_replan"]
                ),
                context_references=(),
                actor=state["value"].actor,
                budget=AgentBudget(
                    max_model_attempts=runtime.max_model_attempts,
                    max_input_tokens=runtime.max_input_tokens,
                    max_output_tokens=runtime.max_output_tokens,
                    max_iterations=runtime.max_iterations,
                    max_tool_calls=runtime.max_tool_calls,
                    max_handoffs=runtime.max_handoffs,
                    max_replans=runtime.max_replans,
                    timeout_seconds=runtime.timeout_seconds,
                ),
                step_id=step.step_id,
                idempotency_key=f"{state['value'].turn_id}:{step.step_id}",
            )
            try:
                self._guard.authorize_handoff(
                    current_actor=actor,
                    parent_agent_id=AgentId.ORCHESTRATOR,
                    handoff=handoff,
                    manifest=registered.manifest,
                )
            except AgentPolicyError:
                return self._failure("HANDOFF_POLICY_REJECTED")
            handoffs.append(handoff)
        try:
            results = await asyncio.gather(
                *(self._specialists.run_specialist(handoff) for handoff in handoffs)
            )
        except Exception:
            return self._failure("SPECIALIST_EXECUTION_FAILED")
        if any(
            result.agent_id is not handoff.target_agent_id
            or result.agent_version != handoff.target_agent_version
            for handoff, result in zip(handoffs, results, strict=True)
        ):
            return self._failure("SPECIALIST_RESULT_IDENTITY_MISMATCH")
        return {
            "last_batch_handoffs": tuple(handoffs),
            "last_batch_results": tuple(results),
            "handoffs_used": state["handoffs_used"] + len(handoffs),
            "route": "execute",
        }

    @staticmethod
    def _trusted_specialist_input(
        step: ExecutionStep, value: OrchestratorInput, risk_replan: RiskReplanRequest | None = None
    ) -> dict[str, JsonValue]:
        """Reconstruct mutation contracts from trusted turn/card context."""
        if step.target_agent_id is AgentId.RISK:
            return {
                "task_reference": step.typed_input.get("task_reference", ""),
                "locale": value.locale,
                "question": value.message,
            }
        if step.target_agent_id is AgentId.DAILY_UPDATE:
            daily = value.active_context.daily_update
            if daily is None:
                raise ValueError("DAILY_UPDATE_TRUSTED_CONTEXT_REQUIRED")
            return cast(dict[str, JsonValue], daily.model_dump(mode="json"))
        if step.target_agent_id is AgentId.ASSIGNMENT:
            active_team = value.active_context.active_team
            exact = value.active_context.exact_assignment
            if step.capability == "assignment.revise_team" and active_team is not None:
                return {
                    "operation": "REVISE_TEAM",
                    "locale": value.locale,
                    "project_id": str(active_team.project_id),
                    "recommendation_id": str(active_team.recommendation_id),
                    "recommendation_version": active_team.recommendation_version,
                    "revision_instruction": value.message,
                }
            if step.capability == "assignment.assign_task_explicitly" and exact is not None:
                return {
                    "operation": "ASSIGN_TASK_EXPLICITLY",
                    "locale": value.locale,
                    "task_id": str(exact.task_id),
                    "task_version": exact.task_version,
                    "membership_id": str(exact.membership_id),
                }
            if active_team is not None and step.capability in {
                "assignment.recommend_team",
                "assignment.analyze_workload",
            }:
                return {
                    "operation": (
                        "RECOMMEND_TEAM"
                        if step.capability == "assignment.recommend_team"
                        else "ANALYZE_WORKLOAD"
                    ),
                    "locale": value.locale,
                    "project_id": str(active_team.project_id),
                    "planning_proposal_id": (
                        str(active_team.planning_proposal_id)
                        if active_team.planning_proposal_id is not None
                        else None
                    ),
                    "planning_proposal_version": active_team.planning_proposal_version,
                }
            return {}
        if step.target_agent_id is not AgentId.PLANNING:
            return step.typed_input
        base: dict[str, JsonValue] = {"locale": value.locale, "brief": value.message}
        if step.capability == "planning.revise" and risk_replan is not None:
            return {
                "operation": "REVISE",
                **base,
                "manager_instruction": value.message,
                "risk_context": cast(dict[str, JsonValue], risk_replan.model_dump(mode="json")),
            }
        if step.capability == "planning.create":
            return {"operation": "CREATE", **base}
        active = value.active_context.active_planning
        if active is None:
            return {"operation": "EXPLAIN", **base}
        references: dict[str, JsonValue] = {
            "workflow_run_id": str(active.workflow_run_id),
            **base,
        }
        if step.capability == "planning.resume":
            return {
                "operation": "RESUME_INPUT",
                **references,
                "manager_instruction": value.message,
            }
        if step.capability == "planning.revise":
            return {
                "operation": "REVISE",
                **references,
                "proposal_id": str(active.proposal_id) if active.proposal_id else None,
                "expected_proposal_version": active.proposal_version,
                "manager_instruction": value.message,
            }
        return {
            "operation": "EXPLAIN",
            **references,
            "proposal_id": str(active.proposal_id) if active.proposal_id else None,
        }

    async def observe_and_update_plan(self, state: OrchestratorState) -> dict[str, object]:
        results = state["last_batch_results"]
        if not results:
            return self._failure("SPECIALIST_EXECUTION_FAILED")
        if any(
            result.status in {AgentRunStatus.FAILED, AgentRunStatus.CANCELLED} for result in results
        ):
            return self._failure("SPECIALIST_RESULT_FAILED")
        completed = (
            *state["completed_step_ids"],
            *(handoff.step_id for handoff in state["last_batch_handoffs"]),
        )
        updates: dict[str, object] = {
            "results": (*state["results"], *results),
            "completed_step_ids": completed,
        }
        if self._combined_planning_started(state, results):
            return {**updates, "route": "human_gate"}
        awaiting_input = next(
            (result for result in results if result.status is AgentRunStatus.AWAITING_INPUT), None
        )
        if awaiting_input is not None:
            return {**updates, "route": "ask_user"}
        if any(
            result.status is AgentRunStatus.AWAITING_HUMAN
            or any(action.requires_human_gate for action in result.proposed_actions)
            for result in results
        ):
            return {**updates, "route": "human_gate"}
        risk = next((r for r in results if r.agent_id is AgentId.RISK), None)
        if risk is not None:
            if risk.requested_handoff is None:
                return {**updates, "route": "next"}
            actor = await self._actor_resolver.resolve(state["value"].actor)
            try:
                card = RiskCardContent.model_validate(risk.typed_output)
                binding = RiskReplanRequest.model_validate(risk.requested_handoff.typed_input)
                if (
                    actor.role not in {"MANAGER", "ADMIN"}
                    or not actor.is_active
                    or card.scope != "MANAGER"
                    or card.state != "READY"
                    or card.score is None
                    or binding.risk_assessment_id != card.risk_assessment_id
                    or binding.task_id != card.task_id
                    or binding.fingerprint != card.fingerprint
                    or binding.observation_ids != tuple(o.id for o in card.observations)
                    or binding.affected_week_ids != card.affected_week_ids
                    or risk.requested_handoff.target_capability != "planning.revise"
                    or not _has_revision_signal(state["value"].message)
                ):
                    raise ValueError("RISK_REPLAN_CONTEXT_INVALID")
                plan = state["plan"]
                if plan is None or state["replans_used"] >= _MAX_REPLANS:
                    raise ValueError("RISK_REPLAN_BUDGET")
                registered = self._registry.resolve(AgentId.PLANNING, "1.0.0", _ACTIVE_PHASE)
                candidate = plan.model_copy(
                    update={
                        "steps": (
                            *plan.steps,
                            ExecutionStep(
                                step_id="risk-replan",
                                target_agent_id=AgentId.PLANNING,
                                target_agent_version=registered.manifest.agent.version,
                                capability="planning.revise",
                                objective=state["value"].message,
                                typed_input={},
                                depends_on=(state["last_batch_handoffs"][0].step_id,),
                                mode=StepMode.PROPOSAL,
                            ),
                        )
                    }
                )
                validate_execution_plan(candidate, self._registry, actor)
            except (ValueError, ExecutionPlanError):
                return {**updates, "route": "next"}
            return {
                **updates,
                "route": "next",
                "plan": candidate,
                "risk_replan": binding,
                "replans_used": state["replans_used"] + 1,
            }
        requested = tuple(
            result.requested_handoff for result in results if result.requested_handoff is not None
        )
        if len(requested) > 1:
            return {**updates, **self._failure("MULTIPLE_HANDOFF_REQUESTS")}
        if requested:
            if state["replans_used"] >= _MAX_REPLANS:
                return {**updates, **self._failure("REPLAN_BUDGET_EXHAUSTED")}
            return {
                **updates,
                "route": "replan",
                "prior_plan": state["plan"],
                "pending_requested_handoff": requested[0],
            }
        return {**updates, "route": "next"}

    async def bounded_repair(self, state: OrchestratorState) -> dict[str, object]:
        if state["pending_requested_handoff"] is not None:
            return {"replans_used": state["replans_used"] + 1}
        return {"repair_attempts": state["repair_attempts"] + 1}

    async def synthesize(self, state: OrchestratorState) -> dict[str, object]:
        plan = state["plan"]
        if plan is None:
            return self._failure("EXECUTION_PLAN_MISSING")
        # Preserve scoped Work answers in evidence blocks so replay can redact them.
        for result in state["results"]:
            raw_evidence = result.typed_output.get("evidence", [])
            has_risk_context = isinstance(raw_evidence, list) and any(
                isinstance(item, dict) and item.get("resource_type") == "RISK_CONTEXT"
                for item in raw_evidence
            )
            if result.agent_id is AgentId.WORK_INTELLIGENCE and has_risk_context:
                from work_management_ai.agents.work_intelligence.contracts import (
                    WorkIntelligenceOutput,
                )

                answer = WorkIntelligenceOutput.model_validate(result.typed_output)
                if any(e.resource_type == "RISK_CONTEXT" for e in answer.evidence):
                    summary = "\n".join(c.text for c in answer.claims)
                    if summary:
                        return {
                            "blocks": (
                                WorkEvidenceResponseBlock(
                                    summary=summary,
                                    evidence=tuple(
                                        PublicEvidenceReference(
                                            evidence_id=e.evidence_id,
                                            resource_type=e.resource_type,
                                            resource_id=e.resource_id,
                                            version=e.resource_version,
                                        )
                                        for e in answer.evidence
                                    ),
                                ),
                            ),
                            "route": "execute",
                        }
        risk_blocks = tuple(
            RiskResponseBlock(
                task_id=UUID(str(r.typed_output["task_id"])),
                fingerprint=str(r.typed_output["fingerprint"]),
                content=r.typed_output,
            )
            for r in state["results"]
            if r.agent_id is AgentId.RISK and r.status is AgentRunStatus.COMPLETED
        )
        if risk_blocks:
            return {"blocks": risk_blocks, "route": "execute"}
        daily_blocks = self._daily_update_blocks(state["results"])
        if daily_blocks:
            return {"blocks": daily_blocks, "route": "execute"}
        assignment_blocks = self._assignment_blocks(state["results"])
        if assignment_blocks:
            return {"blocks": assignment_blocks, "route": "execute"}
        planning_blocks = self._planning_blocks(state["results"])
        if planning_blocks:
            return {"blocks": planning_blocks, "route": "execute"}
        request = StructuredModelRequest(
            invocation_key=f"orchestrator.{state['value'].locale}.synthesize",
            messages=build_synthesis_messages(state["value"], plan, state["results"]),
            output_schema=OrchestratorSynthesis,
            timeout_seconds=60,
        )
        try:
            response = await self._model_gateway.generate_structured(request)
        except ModelGatewayError:
            return {"route": "manual_fallback", "stop_reason": "MODEL_SYNTHESIS_INVALID"}
        blocks = tuple(
            block
            for block in response.parsed.blocks
            if isinstance(
                block,
                (TextResponseBlock, ActivityResponseBlock, WorkEvidenceResponseBlock),
            )
        )
        return {
            "blocks": blocks,
            "model_refs": (*state["model_refs"], response.model_ref),
            "route": "execute",
        }

    async def verify_response(self, state: OrchestratorState) -> dict[str, object]:
        if not state["blocks"]:
            return {
                "blocks": (
                    SafeErrorResponseBlock(
                        code="ORCHESTRATOR_MANUAL_FALLBACK",
                        message_key="ai.error.manualFallback",
                    ),
                ),
                "status": OrchestratorStatus.FAILED,
                "stop_reason": "EMPTY_ORCHESTRATOR_RESPONSE",
            }
        return {"status": OrchestratorStatus.COMPLETED, "stop_reason": "COMPLETED"}

    async def ask_user(self, state: OrchestratorState) -> dict[str, object]:
        if state["stop_reason"] == "DAILY_UPDATE_CLARIFICATION_REQUIRED" or any(
            r.agent_id is AgentId.DAILY_UPDATE for r in state["last_batch_results"]
        ):
            question = (
                "Vui lòng chỉ rõ một công việc được giao, tiến độ % và việc đã làm."
                if state["value"].locale == "vi"
                else "Please specify one assigned task, progress percent and work done."
            )
            return {
                "blocks": (
                    QuestionResponseBlock(
                        question=question, response_context={"daily_update": True}
                    ),
                ),
                "status": OrchestratorStatus.AWAITING_INPUT,
                "stop_reason": "DAILY_UPDATE_NEEDS_INPUT",
            }
        issue = state["value"].active_context.assignment_resolution_issue
        if issue is not None:
            locale = state["value"].locale
            if issue == "ASSIGNMENT_FORBIDDEN":
                return {
                    "blocks": (
                        CapabilityUnavailableResponseBlock(
                            capability="assignment.assign_task_explicitly",
                            message_key="ai.assignment.forbidden",
                        ),
                    ),
                    "status": OrchestratorStatus.COMPLETED,
                    "stop_reason": "ASSIGNMENT_FORBIDDEN",
                }
            if issue == "TASK_AMBIGUOUS_OR_NOT_FOUND":
                question = (
                    "Vui lòng chỉ rõ đúng công việc cần giao."
                    if locale == "vi"
                    else "Please specify the exact task to assign."
                )
            elif issue == "MEMBER_AMBIGUOUS_OR_NOT_FOUND":
                question = (
                    "Vui lòng chỉ rõ đúng thành viên cần giao việc."
                    if locale == "vi"
                    else "Please specify the exact member to assign."
                )
            else:
                question = (
                    "Vui lòng chỉ rõ đúng dự án."
                    if locale == "vi"
                    else "Please specify the exact project."
                )
            return {
                "blocks": (
                    QuestionResponseBlock(
                        question=question,
                        response_context={"source": "assignment_resolution"},
                    ),
                ),
                "status": OrchestratorStatus.AWAITING_INPUT,
                "stop_reason": "ASSIGNMENT_CLARIFICATION_REQUIRED",
            }
        result = state["last_batch_results"][0]
        question_value = result.typed_output.get("question")
        question = (
            question_value if isinstance(question_value, str) else "Additional input is required."
        )
        return {
            "blocks": (
                QuestionResponseBlock(question=question, response_context={"source": "specialist"}),
            ),
            "status": OrchestratorStatus.AWAITING_INPUT,
            "stop_reason": "AWAITING_INPUT",
        }

    async def human_gate(self, state: OrchestratorState) -> dict[str, object]:
        daily_blocks = self._daily_update_blocks(state["last_batch_results"])
        if daily_blocks:
            return {
                "blocks": daily_blocks,
                "status": OrchestratorStatus.AWAITING_HUMAN,
                "stop_reason": "DAILY_UPDATE_AWAIT_OWNER",
            }
        if state["risk_replan"] is not None:
            planning = next(
                (r for r in state["last_batch_results"] if r.agent_id is AgentId.PLANNING), None
            )
            if planning is not None:
                risk = next(r for r in state["results"] if r.agent_id is AgentId.RISK)
                return {
                    "blocks": (
                        RiskResponseBlock(
                            task_id=UUID(str(risk.typed_output["task_id"])),
                            fingerprint=str(risk.typed_output["fingerprint"]),
                            content=risk.typed_output,
                        ),
                        PlanningRunResponseBlock(
                            workflow_run_id=UUID(str(planning.typed_output["workflow_run_id"])),
                            status=str(planning.typed_output["workflow_status"]),
                        ),
                    ),
                    "status": OrchestratorStatus.AWAITING_HUMAN,
                    "stop_reason": "RISK_PLAN_AWAIT_MANAGER",
                }
        pending = self._pending_followup(state)
        if pending is not None and pending.state == "WAITING_PROJECT_PROPOSAL":
            result = state["last_batch_results"][0]
            return {
                "blocks": (
                    PlanningRunResponseBlock(
                        workflow_run_id=pending.planning_workflow_run_id,
                        status=str(result.typed_output.get("workflow_status", "QUEUED")),
                    ),
                ),
                "status": OrchestratorStatus.AWAITING_HUMAN,
                "stop_reason": "WAITING_PROJECT_PROPOSAL",
            }
        assignment_blocks = self._assignment_blocks(state["last_batch_results"])
        if assignment_blocks:
            return {
                "blocks": assignment_blocks,
                "status": OrchestratorStatus.AWAITING_HUMAN,
                "stop_reason": "AWAITING_HUMAN",
            }
        return {
            "blocks": (
                QuestionResponseBlock(
                    question="Human review is required before continuing.",
                    response_context={"gate": "human_review"},
                ),
            ),
            "status": OrchestratorStatus.AWAITING_HUMAN,
            "stop_reason": "AWAITING_HUMAN",
        }

    @staticmethod
    def _daily_update_blocks(results: tuple[AgentResult, ...]) -> tuple[ResponseBlock, ...]:
        return tuple(
            DailyUpdateResponseBlock.model_validate({"kind": "daily_update", **r.typed_output})
            for r in results
            if r.agent_id is AgentId.DAILY_UPDATE and r.status is AgentRunStatus.AWAITING_HUMAN
        )

    @staticmethod
    def _assignment_blocks(results: tuple[AgentResult, ...]) -> tuple[ResponseBlock, ...]:
        blocks: list[ResponseBlock] = []
        for result in results:
            if result.agent_id is not AgentId.ASSIGNMENT:
                continue
            deterministic = result.typed_output.get("deterministic_result")
            operation = result.typed_output.get("operation")
            if not isinstance(deterministic, dict):
                continue
            if deterministic.get("kind") == "team_requirements_pending":
                blocks.append(
                    SafeErrorResponseBlock(
                        code="TEAM_REQUIREMENTS_CONFIRMATION_REQUIRED",
                        message_key="ai.assignment.teamRequirementsConfirmationRequired",
                        manual_fallback="PROJECT_TEAM_EDITOR",
                    )
                )
                continue
            if operation in {"RECOMMEND_TEAM", "REVISE_TEAM"}:
                try:
                    blocks.append(
                        TeamRecommendationResponseBlock.model_validate(
                            {
                                "project_id": deterministic["project_id"],
                                "recommendation_id": deterministic["recommendation_id"],
                                "recommendation_version": deterministic["version"],
                                "status": deterministic["status"],
                                "explanation_status": result.typed_output.get(
                                    "explanation_status", "UNAVAILABLE"
                                ),
                            }
                        )
                    )
                except (KeyError, ValueError):
                    continue
            elif operation == "ASSIGN_TASK_EXPLICITLY":
                try:
                    blocks.append(
                        AssignmentResultResponseBlock.model_validate(
                            {
                                "task_id": deterministic["task_id"],
                                "task_version": deterministic["task_version"],
                                "membership_id": deterministic["membership_id"],
                                "warning_codes": deterministic.get("warning_codes", []),
                            }
                        )
                    )
                except (KeyError, ValueError):
                    continue
        return tuple(blocks)

    @staticmethod
    def _planning_blocks(results: tuple[AgentResult, ...]) -> tuple[ResponseBlock, ...]:
        blocks: list[ResponseBlock] = []
        for result in results:
            if result.agent_id is not AgentId.PLANNING:
                continue
            try:
                blocks.append(
                    PlanningRunResponseBlock(
                        workflow_run_id=UUID(str(result.typed_output["workflow_run_id"])),
                        status=str(result.typed_output["workflow_status"]),
                    )
                )
            except (KeyError, ValueError):
                continue
        return tuple(blocks)

    async def capability_unavailable(self, state: OrchestratorState) -> dict[str, object]:
        plan = state["plan"]
        if plan is None:
            return self._failure("EXECUTION_PLAN_MISSING")
        blocks = tuple(
            CapabilityUnavailableResponseBlock(
                capability=capability,
                message_key="ai.capability.unavailable",
            )
            for capability in plan.unavailable_capabilities
        )
        return {
            "blocks": blocks,
            "status": OrchestratorStatus.COMPLETED,
            "stop_reason": "CAPABILITY_UNAVAILABLE",
        }

    async def manual_fallback(self, state: OrchestratorState) -> dict[str, object]:
        manual_fallback = (
            "PROJECT_TEAM_EDITOR" if state["value"].active_context.active_team is not None else None
        )
        return {
            "blocks": (
                SafeErrorResponseBlock(
                    code="ORCHESTRATOR_MANUAL_FALLBACK",
                    message_key="ai.error.manualFallback",
                    manual_fallback=manual_fallback,
                ),
            ),
            "status": OrchestratorStatus.FAILED,
            "stop_reason": state["stop_reason"],
        }

    async def persistable_result(self, state: OrchestratorState) -> dict[str, object]:
        return {
            "output": OrchestratorOutput(
                execution_plan=state["plan"],
                agent_results=state["results"],
                blocks=state["blocks"],
                completed_step_ids=state["completed_step_ids"],
                status=state["status"],
                stop_reason=state["stop_reason"],
                replans_used=state["replans_used"],
                model_refs=state["model_refs"],
                pending_followup=self._pending_followup(state),
            )
        }

    @staticmethod
    def _pending_followup(state: OrchestratorState) -> PendingFollowup | None:
        plan = state["plan"]
        if plan is None:
            return None
        planning_step_ids = {
            step.step_id for step in plan.steps if step.capability == "planning.create"
        }
        has_dependent_team_step = any(
            step.capability == "assignment.recommend_team"
            and bool(planning_step_ids.intersection(step.depends_on))
            for step in plan.steps
        )
        if not has_dependent_team_step:
            return None
        result = next(
            (
                item
                for item in state["results"]
                if item.agent_id is AgentId.PLANNING
                and item.status in {AgentRunStatus.COMPLETED, AgentRunStatus.AWAITING_HUMAN}
            ),
            None,
        )
        if result is None:
            return None
        try:
            workflow_run_id = UUID(str(result.typed_output["workflow_run_id"]))
            proposal_id = result.typed_output.get("proposal_id")
            proposal_version = result.typed_output.get("proposal_version")
            if proposal_id is None or proposal_version is None:
                return PendingFollowup(
                    planning_workflow_run_id=workflow_run_id,
                    state="WAITING_PROJECT_PROPOSAL",
                )
            return PendingFollowup(
                planning_workflow_run_id=workflow_run_id,
                planning_proposal_id=UUID(str(proposal_id)),
                planning_proposal_version=int(str(proposal_version)),
                state="WAITING_PROJECT_DECISION",
            )
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _combined_planning_started(
        state: OrchestratorState, results: tuple[AgentResult, ...]
    ) -> bool:
        plan = state["plan"]
        if plan is None:
            return False
        planning_steps = {
            step.step_id for step in plan.steps if step.capability == "planning.create"
        }
        if not any(
            step.capability == "assignment.recommend_team"
            and bool(planning_steps.intersection(step.depends_on))
            for step in plan.steps
        ):
            return False
        return any(
            result.agent_id is AgentId.PLANNING
            and result.typed_output.get("workflow_run_id") is not None
            for result in results
        )

    @staticmethod
    def _failure(code: str) -> dict[str, object]:
        return {
            "route": "manual_fallback",
            "status": OrchestratorStatus.FAILED,
            "stop_reason": code,
        }


def _has_revision_signal(message: str) -> bool:
    normalized = message.casefold()
    return any(
        re.search(rf"(?<!\w){re.escape(signal)}(?!\w)", normalized) is not None
        for signal in _REVISION_SIGNALS
    )


def _has_combined_project_team_signal(message: str) -> bool:
    normalized = message.casefold()
    has_project = any(token in normalized for token in ("project", "dự án"))
    has_team = any(
        token in normalized
        for token in ("form a team", "create a team", "team", "đội ngũ", "nhóm", "nhân sự")
    )
    return has_project and has_team
