"""Navigation outcomes preserve capability contracts and never grant authority."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from work_management_ai.agents.orchestrator.contracts import ExecutionPlan, OrchestratorGoalQuestion
from work_management_ai.agents.orchestrator.harness import resolve_interaction_outcome
from work_management_ai.runtime.contracts import (
    AgentId,
    AgentResult,
    AgentRunStatus,
    InteractionOutcome,
)


def test_unknown_task_can_ask_before_delegation() -> None:
    plan = ExecutionPlan(
        objectives=("Help me",),
        steps=(),
        response_language="en",
        goal_question=OrchestratorGoalQuestion(question="What would you like to accomplish?"),
    )
    assert not plan.steps and plan.goal_question is not None
    with pytest.raises(ValidationError):
        ExecutionPlan(
            objectives=("Help",),
            unavailable_capabilities=("future",),
            response_language="en",
            goal_question=plan.goal_question,
        )


def test_legacy_result_preserves_projection() -> None:
    result = AgentResult(
        agent_id=AgentId.REPORTING,
        agent_version="1.0.0",
        status=AgentRunStatus.COMPLETED,
        typed_output={"metric": None},
        stop_reason="COMPLETED",
    )
    assert resolve_interaction_outcome(result) is None
    assert result.typed_output == {"metric": None} and not result.proposed_actions


def test_draft_ready_requires_authoritative_binding() -> None:
    result = AgentResult(
        agent_id=AgentId.PLANNING,
        agent_version="1.0.0",
        status=AgentRunStatus.COMPLETED,
        typed_output={
            "workflow_run_id": str(uuid4()),
            "draft_id": str(uuid4()),
            "draft_version": 1,
            "workflow_status": "DRAFT_AVAILABLE",
            "awaiting": "NONE",
        },
        interaction_outcome=InteractionOutcome.DRAFT_READY,
        stop_reason="COMPLETED",
    )
    assert resolve_interaction_outcome(result) is InteractionOutcome.DRAFT_READY
    assert not result.proposed_actions
    with pytest.raises(ValueError):
        resolve_interaction_outcome(result.model_copy(update={"typed_output": {}}))
    with pytest.raises(ValueError):
        resolve_interaction_outcome(
            result.model_copy(
                update={"typed_output": {**result.typed_output, "awaiting": "MANAGER_INPUT"}}
            )
        )


def test_review_outcome_without_exact_proposal_cannot_execute() -> None:
    result = AgentResult(
        agent_id=AgentId.PLANNING,
        agent_version="1.0.0",
        status=AgentRunStatus.COMPLETED,
        typed_output={},
        interaction_outcome=InteractionOutcome.ACTION_REVIEW_REQUIRED,
        stop_reason="COMPLETED",
    )
    with pytest.raises(ValueError):
        resolve_interaction_outcome(result)
