"""Protect sources, immutable user constraints and dependency feasibility."""

import json
from pathlib import Path
from typing import cast

import pytest

from work_management_ai.schemas.progressive_planning import (
    PlanningChangeAssessment,
    PlanningDraft,
    PlanningField,
    PlanningFieldStatus,
)
from work_management_ai.workflows.planning.draft_verifier import (
    DraftVerificationContext,
    verify_draft,
    verify_draft_revision,
)


def draft_data() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads((Path(__file__).parent / "fixtures/progressive_planning/en.json").read_text()),
    )


def context() -> DraftVerificationContext:
    return DraftVerificationContext(permitted_source_refs=frozenset({"message:1"}))


def test_foreign_or_invented_source_is_rejected() -> None:
    draft = PlanningDraft.model_validate(draft_data())
    bad = draft.model_copy(
        update={
            "brief": draft.brief.model_copy(
                update={
                    "objective": PlanningField(
                        value="Secret project",
                        status=PlanningFieldStatus.AUTHORIZED_SOURCE,
                        source_refs=("foreign:2",),
                    )
                }
            )
        }
    )
    assert "SOURCE_NOT_PERMITTED" in [item.code for item in verify_draft(bad, context()).errors]


def test_user_constraint_cannot_be_downgraded_to_assumption() -> None:
    base = PlanningDraft.model_validate(draft_data())
    changed = base.model_copy(
        update={
            "brief": base.brief.model_copy(
                update={
                    "objective": PlanningField(
                        value="Different goal",
                        status=PlanningFieldStatus.AI_ASSUMPTION,
                        assumption_reason="Guessed",
                    )
                }
            )
        }
    )
    result = verify_draft_revision(
        base,
        changed,
        PlanningChangeAssessment(
            affected_field_paths=("brief.budget",), source_refs=("message:1",)
        ),
    )
    assert "USER_CONSTRAINT_CHANGED" in [item.code for item in result.errors]


def test_two_week_horizon_reports_21_day_dependency_chain() -> None:
    data = draft_data()
    data["duration_days"] = 14
    data["phases"] = [{"ref": "p1", "title": "Launch", "start_day": 0, "end_day": 21}]
    data["tasks"] = [
        {
            "ref": f"t{i + 1}",
            "phase_ref": "p1",
            "title": f"Step {i + 1}",
            "start_day": i * 7,
            "end_day": (i + 1) * 7,
            "duration_days": 7,
            "duration_source": "AI_ASSUMPTION",
            "acceptance_criteria": ["Done"],
        }
        for i in range(3)
    ]
    data["dependencies"] = [
        {"predecessor_ref": "t1", "successor_ref": "t2"},
        {"predecessor_ref": "t2", "successor_ref": "t3"},
    ]
    result = verify_draft(PlanningDraft.model_validate(data), context())
    assert not result.can_prepare
    assert result.conflicts[0].code == "HORIZON_DEPENDENCY_CONFLICT"
    assert result.conflicts[0].requested_days == 14
    assert result.conflicts[0].minimum_days == 21
    assert result.conflicts[0].chain_refs == ("t1", "t2", "t3")


def test_overlapping_phases_do_not_increase_elapsed_duration() -> None:
    data = draft_data()
    data["phases"] = [
        {"ref": "p1", "title": "Prepare", "start_day": 0, "end_day": 28},
        {"ref": "p2", "title": "Market", "start_day": 14, "end_day": 42},
    ]
    assert verify_draft(PlanningDraft.model_validate(data), context()).can_prepare


@pytest.mark.parametrize(
    "dependencies,code",
    [
        ([{"predecessor_ref": "t1", "successor_ref": "missing"}], "DEPENDENCY_REF_UNKNOWN"),
        ([{"predecessor_ref": "t1", "successor_ref": "t1"}], "DEPENDENCY_CYCLE"),
    ],
)
def test_invalid_graph_blocks_preparation(dependencies: list[dict[str, str]], code: str) -> None:
    data = draft_data()
    data["dependencies"] = dependencies
    result = verify_draft(PlanningDraft.model_validate(data), context())
    assert code in [item.code for item in result.errors]
    assert not result.can_prepare


def test_assumed_goal_cannot_be_passed_off_as_a_useful_draft() -> None:
    draft = PlanningDraft.model_validate(draft_data())
    guessed = draft.model_copy(
        update={
            "brief": draft.brief.model_copy(
                update={
                    "objective": PlanningField(
                        value="Complete a controlled process",
                        status=PlanningFieldStatus.AI_ASSUMPTION,
                        assumption_reason="No actual goal supplied",
                    )
                }
            )
        }
    )
    result = verify_draft(guessed, context())
    assert "GOAL_REQUIRED" in [item.code for item in result.errors]
    assert not result.can_prepare


def test_horizon_edit_cannot_downgrade_user_stated_task_duration() -> None:
    base = PlanningDraft.model_validate(draft_data())
    task = base.tasks[0].model_copy(
        update={"duration_source": "USER_STATED", "duration_source_refs": ("message:1",)}
    )
    base = base.model_copy(update={"tasks": (task,)})
    changed = base.model_copy(
        update={
            "tasks": (
                task.model_copy(
                    update={
                        "duration_days": 1,
                        "end_day": 1,
                        "duration_source": "AI_ASSUMPTION",
                        "duration_source_refs": (),
                    }
                ),
            )
        }
    )
    assessment = PlanningChangeAssessment(
        affected_field_paths=("duration_days",),
        affected_task_refs=(task.ref,),
        source_refs=("message:2",),
    )
    assert "USER_TASK_DURATION_CHANGED" in {
        error.code for error in verify_draft_revision(base, changed, assessment).errors
    }


def test_task_cannot_be_scheduled_outside_its_phase() -> None:
    base = PlanningDraft.model_validate(draft_data())
    task = base.tasks[0].model_copy(
        update={"start_day": 20, "end_day": 20 + base.tasks[0].duration_days}
    )
    changed = base.model_copy(update={"tasks": (task,)})
    assert "TASK_OUTSIDE_PHASE" in {error.code for error in verify_draft(changed, context()).errors}
