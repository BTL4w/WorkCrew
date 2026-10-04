"""Deterministic Planning Tool result verification."""

from work_management_ai.agents.planning.contracts import (
    PlanningAgentInput,
    PlanningAgentOutput,
)


class PlanningResultError(ValueError):
    pass


def verify_planning_result(
    value: PlanningAgentInput,
    output: PlanningAgentOutput,
) -> None:
    if output.operation is not value.operation:
        raise PlanningResultError("PLANNING_OPERATION_MISMATCH")
    if value.workflow_run_id is not None and output.workflow_run_id != value.workflow_run_id:
        raise PlanningResultError("PLANNING_WORKFLOW_MISMATCH")
    if value.proposal_id is not None and output.proposal_id != value.proposal_id:
        raise PlanningResultError("PLANNING_PROPOSAL_MISMATCH")
    queued_risk_revision = (
        value.risk_context is not None
        and output.workflow_status == "QUEUED"
        and output.proposal_id is None
        and output.proposal_version is None
        and output.approval_id is None
    )
    if (
        not queued_risk_revision
        and output.awaiting == "MANAGER_DECISION"
        and (output.proposal_id is None or output.proposal_version is None)
    ):
        raise PlanningResultError("PLANNING_PROPOSAL_REFERENCE_MISSING")
