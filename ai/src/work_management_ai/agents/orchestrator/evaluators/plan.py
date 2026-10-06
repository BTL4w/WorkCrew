"""Deterministic DAG, activation and scope validation."""

from work_management_ai.agents.orchestrator.contracts import (
    ExecutionPlan,
    ExecutionStep,
    StepMode,
)
from work_management_ai.runtime.agent_registry import AgentRegistry, AgentRegistryError
from work_management_ai.runtime.contracts import (
    AgentId,
    RequestedHandoff,
    ResolvedActorContext,
    RiskLevel,
)


class ExecutionPlanError(ValueError):
    pass


def validate_execution_plan(
    plan: ExecutionPlan,
    registry: AgentRegistry,
    actor: ResolvedActorContext,
) -> None:
    if not actor.is_active:
        raise ExecutionPlanError("ACTOR_INACTIVE")
    step_ids = tuple(step.step_id for step in plan.steps)
    if len(step_ids) != len(set(step_ids)):
        raise ExecutionPlanError("DUPLICATE_STEP_ID")
    known_ids = set(step_ids)
    by_id = {step.step_id: step for step in plan.steps}

    for step in plan.steps:
        if step.target_agent_id is AgentId.ORCHESTRATOR:
            raise ExecutionPlanError("ORCHESTRATOR_CANNOT_TARGET_ITSELF")
        if any(dependency not in known_ids for dependency in step.depends_on):
            raise ExecutionPlanError("UNKNOWN_STEP_DEPENDENCY")
        if step.step_id in step.depends_on:
            raise ExecutionPlanError("SELF_STEP_DEPENDENCY")
        try:
            registered = registry.resolve(
                step.target_agent_id,
                step.target_agent_version,
                active_phase=5,
            )
        except AgentRegistryError as exc:
            raise ExecutionPlanError("UNKNOWN_OR_INACTIVE_AGENT") from exc
        if step.target_agent_id is AgentId.REPORTING:
            from work_management_ai.agents.orchestrator.contracts import ReportIntent

            try:
                intent = ReportIntent.model_validate(step.typed_input)
            except ValueError as exc:
                raise ExecutionPlanError("REPORT_INTENT_INVALID") from exc
            expected = (
                ("reporting.prepare_report", StepMode.PROPOSAL)
                if intent.operation == "PREPARE_REPORT"
                else ("reporting.explain_snapshot", StepMode.READ_ONLY)
            )
            if (step.capability, step.mode) != expected:
                raise ExecutionPlanError("REPORT_INTENT_MODE_MISMATCH")
        manifest = registered.manifest
        if step.capability not in manifest.capabilities:
            raise ExecutionPlanError("CAPABILITY_NOT_ALLOWED")
        if actor.role not in manifest.permissions.roles:
            raise ExecutionPlanError("AGENT_ROLE_FORBIDDEN")
        if (
            step.mode is StepMode.PROPOSAL
            and manifest.permissions.risk_ceiling is RiskLevel.READ_ONLY
        ):
            raise ExecutionPlanError("PROPOSAL_MODE_NOT_ALLOWED")
        if step.mode is StepMode.EXPLICIT_WRITE and (
            manifest.permissions.risk_ceiling is not RiskLevel.EXPLICIT_WRITE
            or actor.role not in {"ADMIN", "MANAGER"}
        ):
            raise ExecutionPlanError("EXPLICIT_WRITE_NOT_ALLOWED")

    _reject_cycles(by_id)
    for step in plan.steps:
        if step.mode is StepMode.READ_ONLY and any(
            by_id[dependency].mode is StepMode.PROPOSAL for dependency in step.depends_on
        ):
            raise ExecutionPlanError("READ_AFTER_PROPOSAL_FORBIDDEN")
    declared = {step.capability for step in plan.steps}
    if declared.intersection(plan.unavailable_capabilities):
        raise ExecutionPlanError("CAPABILITY_BOTH_ACTIVE_AND_UNAVAILABLE")


def _reject_cycles(by_id: dict[str, ExecutionStep]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str) -> None:
        if step_id in visiting:
            raise ExecutionPlanError("EXECUTION_PLAN_CYCLE")
        if step_id in visited:
            return
        visiting.add(step_id)
        for dependency in by_id[step_id].depends_on:
            visit(dependency)
        visiting.remove(step_id)
        visited.add(step_id)

    for step_id in by_id:
        visit(step_id)


def ready_batches(
    plan: ExecutionPlan,
    completed: frozenset[str],
) -> tuple[tuple[ExecutionStep, ...], ...]:
    ready = tuple(
        step
        for step in plan.steps
        if step.step_id not in completed and set(step.depends_on).issubset(completed)
    )
    read_only = tuple(step for step in ready if step.mode is StepMode.READ_ONLY)
    if read_only:
        return (read_only,)
    proposals = tuple(step for step in ready if step.mode is StepMode.PROPOSAL)
    if proposals:
        return ((proposals[0],),)
    explicit_writes = tuple(step for step in ready if step.mode is StepMode.EXPLICIT_WRITE)
    if explicit_writes:
        return ((explicit_writes[0],),)
    return ()


def validate_replan(
    *,
    prior: ExecutionPlan,
    candidate: ExecutionPlan,
    completed_step_ids: frozenset[str],
    requested_handoff: RequestedHandoff,
) -> None:
    if (
        candidate.objectives != prior.objectives
        or candidate.response_language != prior.response_language
    ):
        raise ExecutionPlanError("REPLAN_SCOPE_BROADENED")
    allowed_capabilities = {step.capability for step in prior.steps}
    allowed_capabilities.add(requested_handoff.target_capability)
    if any(step.capability not in allowed_capabilities for step in candidate.steps):
        raise ExecutionPlanError("REPLAN_SCOPE_BROADENED")
    prior_by_id = {step.step_id: step for step in prior.steps}
    candidate_by_id = {step.step_id: step for step in candidate.steps}
    for completed_id in completed_step_ids:
        if candidate_by_id.get(completed_id) != prior_by_id.get(completed_id):
            raise ExecutionPlanError("REPLAN_CHANGED_COMPLETED_STEP")
