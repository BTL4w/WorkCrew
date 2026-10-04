"""Immutable source binding and bounded weekly scheduling changes."""

from datetime import date
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskPlanBinding(Contract):
    task_id: UUID
    risk_assessment_id: UUID
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    observation_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    affected_week_ids: tuple[UUID, ...] = Field(max_length=52)


class WeekSource(Contract):
    id: UUID
    version: int
    week_number: int
    start_date: date
    end_date: date
    objective: str
    status: str
    baseline_id: UUID | None
    baseline_sequence: int | None


class TaskSource(Contract):
    id: UUID
    version: int
    project_week_id: UUID | None
    title: str
    description: str | None
    due_date: date | None
    estimated_effort_hours: int | None
    assignee_membership_id: UUID | None
    status: str
    milestone_target_date: date | None = None
    required_skill_labels: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]


class PlanSource(Contract):
    project_id: UUID
    project_version: int
    project_title: str
    project_description: str | None
    weeks: tuple[WeekSource, ...] = Field(min_length=1, max_length=52)
    tasks: tuple[TaskSource, ...] = Field(max_length=100)


class RiskPlanMetadata(Contract):
    binding: RiskPlanBinding
    before: PlanSource


class TaskChange(Contract):
    task_id: UUID
    project_week_id: UUID
    due_date: date | None
    estimated_effort_hours: int = Field(ge=1, le=10000)


class NewTask(Contract):
    project_week_id: UUID
    title: str = Field(min_length=1, max_length=160)
    description: str | None = Field(max_length=4000)
    due_date: date | None
    estimated_effort_hours: int = Field(ge=1, le=10000)
    acceptance_criteria: tuple[str, ...] = Field(min_length=1, max_length=20)


class RiskWeeklyDraft(Contract):
    task_changes: tuple[TaskChange, ...] = Field(max_length=100)
    new_tasks: tuple[NewTask, ...] = Field(max_length=20)
    change_summary: str = Field(min_length=1, max_length=2000)


class ScheduledTask(Contract):
    ref: str
    project_week_ref: str
    milestone_ref: str | None
    title: str
    description: str | None
    due_date: str | None
    assignee_membership_id: str | None
    required_skill_labels: list[str]
    estimated_effort_hours: int = Field(strict=True, ge=1, le=10000)
    acceptance_criteria: list[str]


def base_content(source: PlanSource) -> dict[str, Any]:
    if any(t.estimated_effort_hours is None or t.project_week_id is None for t in source.tasks):
        raise ValueError("RISK_PLAN_INCOMPLETE")
    return {
        "project": {
            "title": source.project_title,
            "description": source.project_description,
            "start_date": None,
            "due_date": None,
        },
        "goal": {
            "title": source.project_title,
            "description": None,
            "expected_outcomes": [],
            "target_date": None,
        },
        "milestones": [],
        "project_weeks": [
            dict(
                ref=str(w.id),
                week_number=w.week_number,
                start_date=w.start_date.isoformat(),
                end_date=w.end_date.isoformat(),
                objective=w.objective,
            )
            for w in source.weeks
        ],
        "tasks": [
            dict(
                ref=str(t.id),
                project_week_ref=str(t.project_week_id),
                milestone_ref=None,
                title=t.title,
                description=t.description,
                due_date=t.due_date.isoformat() if t.due_date else None,
                assignee_membership_id=None,
                required_skill_labels=list(t.required_skill_labels),
                estimated_effort_hours=t.estimated_effort_hours,
                acceptance_criteria=list(t.acceptance_criteria),
            )
            for t in source.tasks
        ],
        "dependencies": [],
        "assumptions": [],
    }


def draft_content(metadata: RiskPlanMetadata, draft: RiskWeeklyDraft) -> dict[str, Any]:
    content = base_content(metadata.before)
    by_id = {str(t["ref"]): t for t in content["tasks"]}
    if len({c.task_id for c in draft.task_changes}) != len(draft.task_changes):
        raise ValueError("DUPLICATE_TASK_CHANGE")
    for change in draft.task_changes:
        task = by_id.get(str(change.task_id))
        if task is None:
            raise ValueError("TASK_OUTSIDE_PLAN")
        task.update(
            project_week_ref=str(change.project_week_id),
            due_date=change.due_date.isoformat() if change.due_date else None,
            estimated_effort_hours=change.estimated_effort_hours,
        )
    for index, task in enumerate(draft.new_tasks):
        content["tasks"].append(
            dict(
                ref=f"new:{index}",
                project_week_ref=str(task.project_week_id),
                milestone_ref=None,
                title=task.title,
                description=task.description,
                due_date=task.due_date.isoformat() if task.due_date else None,
                assignee_membership_id=None,
                required_skill_labels=[],
                estimated_effort_hours=task.estimated_effort_hours,
                acceptance_criteria=list(task.acceptance_criteria),
            )
        )
    content["risk_replan"] = metadata.model_dump(mode="json")
    return validate_content(content)


def validate_content(content: dict[str, Any]) -> dict[str, Any]:
    metadata = RiskPlanMetadata.model_validate(content["risk_replan"])
    baseline = base_content(metadata.before)
    if set(content) != set(baseline) | {"risk_replan"}:
        raise ValueError("RISK_PLAN_FIELDS_INVALID")
    for key in baseline:
        if key != "tasks" and content[key] != baseline[key]:
            raise ValueError("WEEKLY_REPLAN_SCOPE_INVALID")
    raw_tasks: object = content["tasks"]
    if not isinstance(raw_tasks, list) or not 1 <= len(cast(list[object], raw_tasks)) <= 100:
        raise ValueError("TASK_LIMIT_EXCEEDED")
    tasks = [ScheduledTask.model_validate(t).model_dump() for t in cast(list[object], raw_tasks)]
    before = {str(t.id): t for t in metadata.before.tasks}
    original = {t["ref"]: t for t in baseline["tasks"]}
    weeks = {str(w.id): w for w in metadata.before.weeks}
    refs: set[str] = set()
    changed = False
    for task in tasks:
        if set(task) != set(
            next(
                iter(original.values()),
                dict(
                    ref=None,
                    project_week_ref=None,
                    milestone_ref=None,
                    title=None,
                    description=None,
                    due_date=None,
                    assignee_membership_id=None,
                    required_skill_labels=None,
                    estimated_effort_hours=None,
                    acceptance_criteria=None,
                ),
            )
        ):
            raise ValueError("TASK_FIELDS_INVALID")
        ref = task["ref"]
        if ref in refs:
            raise ValueError("DUPLICATE_TASK_REF")
        refs.add(ref)
        if task["assignee_membership_id"] is not None:
            raise ValueError("ASSIGNEE_NOT_ALLOWED_IN_PLAN")
        week = weeks.get(task["project_week_ref"])
        if week is None:
            raise ValueError("TASK_WEEK_OUTSIDE_PROJECT")
        if ref in original:
            old = original[ref]
            for key in old:
                if (
                    key not in {"project_week_ref", "due_date", "estimated_effort_hours"}
                    and task[key] != old[key]
                ):
                    raise ValueError("EXISTING_TASK_FIELD_PROTECTED")
            if task == old:
                continue
            if before[ref].status == "DONE" or weeks[old["project_week_ref"]].status == "COMPLETED":
                raise ValueError("COMPLETED_WORK_PROTECTED")
        elif not isinstance(ref, str) or not ref.startswith("new:"):
            raise ValueError("TASK_REF_INVALID")
        elif task["milestone_ref"] is not None or task["required_skill_labels"]:
            raise ValueError("NEW_TASK_SCOPE_INVALID")
        if ref not in original:
            criteria = task["acceptance_criteria"]
            if (
                not task["title"].strip()
                or any(not 1 <= len(c.strip()) <= 1000 for c in criteria)
                or len({c.strip() for c in criteria}) != len(criteria)
            ):
                raise ValueError("NEW_TASK_TEXT_INVALID")
        if week.status == "COMPLETED":
            raise ValueError("COMPLETED_WORK_PROTECTED")
        NewTask.model_validate(
            {
                k: task[k]
                for k in (
                    "title",
                    "description",
                    "due_date",
                    "estimated_effort_hours",
                    "acceptance_criteria",
                )
            }
            | {"project_week_id": week.id}
        ) if ref not in original else TaskChange.model_validate(
            {
                "task_id": ref,
                "project_week_id": week.id,
                "due_date": task["due_date"],
                "estimated_effort_hours": task["estimated_effort_hours"],
            }
        )
        due = date.fromisoformat(task["due_date"]) if task["due_date"] else None
        if (
            ref in before
            and before[ref].milestone_target_date is not None
            and due is not None
            and due > cast(date, before[ref].milestone_target_date)
        ):
            raise ValueError("TASK_DATE_OUTSIDE_MILESTONE")
        if due is not None and not week.start_date <= due <= week.end_date:
            raise ValueError("TASK_DATE_OUTSIDE_WEEK")
        changed = True
    if len(refs - set(original)) > 20:
        raise ValueError("NEW_TASK_LIMIT_EXCEEDED")
    if not set(original).issubset(refs):
        raise ValueError("EXISTING_TASK_REMOVAL_DENIED")
    if not changed:
        raise ValueError("RISK_PLAN_NO_CHANGES")
    return content
