"""Only a queued risk revision can omit not-yet-created proposal references."""

from uuid import uuid4

import pytest

from work_management_ai.agents.planning.contracts import PlanningAgentInput, PlanningAgentOutput
from work_management_ai.agents.planning.evaluators.proposal import (
    PlanningResultError,
    verify_planning_result,
)


@pytest.mark.parametrize(
    "scenario", ["risk_queue", "risk_running", "ordinary_create", "partial_refs"]
)
def test_pending_risk_proposal_keeps_exact_human_gate(scenario: str):
    value = PlanningAgentInput.model_validate(
        {
            "operation": "CREATE" if scenario == "ordinary_create" else "REVISE",
            "locale": "en",
            "brief": "Replan delivery",
            "manager_instruction": "Replan delivery",
            "risk_context": None
            if scenario == "ordinary_create"
            else {
                "task_id": str(uuid4()),
                "risk_assessment_id": str(uuid4()),
                "fingerprint": "a" * 64,
                "observation_ids": ["observation:0"],
                "affected_week_ids": [str(uuid4())],
            },
        }
    )
    output = PlanningAgentOutput(
        operation=value.operation,
        workflow_run_id=uuid4(),
        workflow_status="RUNNING" if scenario == "risk_running" else "QUEUED",
        proposal_id=uuid4() if scenario == "partial_refs" else None,
        approval_id=None,
        awaiting="MANAGER_DECISION",
        public_summary="Queued",
    )
    if scenario == "risk_queue":
        verify_planning_result(value, output)
    else:
        with pytest.raises(PlanningResultError, match="PLANNING_PROPOSAL_REFERENCE_MISSING"):
            verify_planning_result(value, output)
