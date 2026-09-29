"""Explicit manual draft and submission contracts."""

from pydantic import Field

from app.modules.progress.domain.daily_updates import DailyUpdateItemInput, ReportingContract


class DailyUpdateDraftRequest(ReportingContract):
    items: tuple[DailyUpdateItemInput, ...] = Field(min_length=1, max_length=50)


class DailyUpdateRevisionRequest(DailyUpdateDraftRequest):
    expected_version: int = Field(ge=1)
