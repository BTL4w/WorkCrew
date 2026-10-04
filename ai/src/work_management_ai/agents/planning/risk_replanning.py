"""Planning-owned model output for a bounded weekly diff, never an approval."""

from datetime import date
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WeeklyTaskChange(Contract):
    task_id: UUID
    project_week_id: UUID
    due_date: date | None
    estimated_effort_hours: int = Field(ge=1, le=10000)


class WeeklyNewTask(Contract):
    project_week_id: UUID
    title: str = Field(min_length=1, max_length=160)
    description: str | None = Field(max_length=4000)
    due_date: date | None
    estimated_effort_hours: int = Field(ge=1, le=10000)
    acceptance_criteria: tuple[str, ...] = Field(min_length=1, max_length=20)


class WeeklyReplanModelOutput(Contract):
    task_changes: tuple[WeeklyTaskChange, ...] = Field(max_length=100)
    new_tasks: tuple[WeeklyNewTask, ...] = Field(max_length=20)
    change_summary: str = Field(min_length=1, max_length=2000)
