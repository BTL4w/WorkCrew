"""Progressive decisions must not turn clarification into an actionable plan."""

import pytest
from pydantic import ValidationError

from work_management_ai.schemas.progressive_planning import (
    PlanningDecision,
    PlanningDraft,
    PlanningField,
    PlanningFieldStatus,
    PlanningQuestion,
)


def draft_data() -> dict[str, object]:
    return {
        "brief": {
            "objective": {
                "value": "Launch an online course",
                "status": "USER_STATED",
                "source_refs": ["message:1"],
            },
            "audience": {
                "value": "Working adults",
                "status": "USER_STATED",
                "source_refs": ["message:1"],
            },
            "expected_outcomes": [
                {"value": "100 learners", "status": "USER_STATED", "source_refs": ["message:1"]}
            ],
            "timing": {
                "value": 42,
                "status": "AI_ASSUMPTION",
                "assumption_reason": "Suggested launch duration",
            },
        },
        "duration_days": 42,
        "phases": [{"ref": "p1", "title": "Prepare course", "start_day": 0, "end_day": 14}],
        "tasks": [
            {
                "ref": "t1",
                "phase_ref": "p1",
                "title": "Prepare course materials",
                "start_day": 0,
                "end_day": 7,
                "duration_days": 7,
                "duration_source": "AI_ASSUMPTION",
                "acceptance_criteria": ["Materials ready"],
            }
        ],
        "assumptions": [
            {
                "value": "Six-week launch",
                "status": "AI_ASSUMPTION",
                "assumption_reason": "No launch date supplied",
            }
        ],
    }


def test_goal_question_has_no_draft() -> None:
    question = PlanningQuestion(id="goal", purpose="GOAL", text="What is your goal?", blocking=True)
    decision = PlanningDecision(kind="ASK_GOAL", question=question)
    assert decision.draft is None
    with pytest.raises(ValidationError):
        PlanningDecision(
            kind="ASK_GOAL", question=question, draft=PlanningDraft.model_validate(draft_data())
        )


def test_draft_allows_optional_time_and_budget() -> None:
    draft = PlanningDraft.model_validate(draft_data())
    assert draft.brief.budget is None
    assert draft.brief.timing.status == "AI_ASSUMPTION"
    assert draft.duration_days == 42


def test_follow_up_does_not_block_draft() -> None:
    question = PlanningQuestion(
        id="date", purpose="FOLLOW_UP", text="Do you have a launch date?", blocking=False
    )
    decision = PlanningDecision(
        kind="DRAFT", draft=PlanningDraft.model_validate(draft_data()), question=question
    )
    assert decision.question is not None and not decision.question.blocking
    with pytest.raises(ValidationError):
        PlanningDecision(
            kind="DRAFT",
            draft=decision.draft,
            question=PlanningQuestion(id="goal", purpose="GOAL", text="What goal?", blocking=True),
        )


def test_assumptions_need_an_explicit_reason() -> None:
    with pytest.raises(ValidationError):
        PlanningField(value="Confirmed funds", status=PlanningFieldStatus.AI_ASSUMPTION)
