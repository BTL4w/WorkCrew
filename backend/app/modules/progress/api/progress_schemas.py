"""Typed weekly progress response, preserving Decimal values and unknowns."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.modules.progress.domain.weekly_progress import WeekActuals


class WeeklyProgressResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    original: WeekActuals
    current_plan: WeekActuals
    added_task_ids: tuple[UUID, ...]
    removed_task_ids: tuple[UUID, ...]
    sealed: bool
