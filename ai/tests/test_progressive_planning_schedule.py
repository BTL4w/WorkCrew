"""Calendar materialization retains relative ordering and exact approval dates."""

import json
from datetime import date
from pathlib import Path

import pytest

from work_management_ai.schemas.progressive_planning import PlanningDraft
from work_management_ai.workflows.planning.draft_schedule import materialize_draft


def draft() -> PlanningDraft:
    return PlanningDraft.model_validate(
        json.loads((Path(__file__).parent / "fixtures/progressive_planning/en.json").read_text())
    )


def test_six_week_calendar_is_concrete_nonoverlapping_and_unassigned() -> None:
    plan = materialize_draft(draft(), date(2026, 10, 12), "Asia/Ho_Chi_Minh")
    assert plan.project.start_date == date(2026, 10, 12)
    assert plan.project.due_date == date(2026, 11, 22)
    assert len(plan.project_weeks) == 6
    assert plan.project_weeks[0].start_date == date(2026, 10, 12)
    assert plan.project_weeks[0].end_date == date(2026, 10, 18)
    assert plan.project_weeks[1].start_date == date(2026, 10, 19)
    assert plan.tasks[0].due_date == date(2026, 10, 18)
    assert plan.tasks[0].assignee_membership_id is None
    assert plan.assumptions and all("TRUSTED" not in item.source for item in plan.assumptions)


def test_conflicted_draft_cannot_be_prepared() -> None:
    raw = draft().model_dump(mode="json")
    raw["duration_days"] = 14
    raw["phases"][0]["end_day"] = 21
    first = raw["tasks"][0]
    raw["tasks"] = [
        {**first, "ref": f"t{i + 1}", "start_day": i * 7, "end_day": (i + 1) * 7} for i in range(3)
    ]
    raw["dependencies"] = [
        {"predecessor_ref": "t1", "successor_ref": "t2"},
        {"predecessor_ref": "t2", "successor_ref": "t3"},
    ]
    with pytest.raises(ValueError, match="HORIZON_DEPENDENCY_CONFLICT"):
        materialize_draft(PlanningDraft.model_validate(raw), date(2026, 10, 12), "Asia/Ho_Chi_Minh")


def test_unknown_timezone_cannot_prepare() -> None:
    with pytest.raises(ValueError, match="timezone"):
        materialize_draft(draft(), date(2026, 10, 12), "arbitrary")
