"""Confirmed daily-summary configuration, independent of HTTP or model SDKs."""

from datetime import datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator


class ScheduleError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScheduleCommand(Contract):
    narrative_mode: Literal["NONE", "DRAFT_FOR_MANAGER"] = "NONE"
    narrative_locale: Literal["vi", "en"] = "vi"
    project_id: UUID
    timezone: str = Field(min_length=1, max_length=64)
    weekdays: tuple[int, ...] = Field(min_length=1, max_length=7)
    cutoff: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    send_when_complete: bool = True
    partial_at_cutoff: bool = True
    recipients: tuple[UUID, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def valid(self) -> "ScheduleCommand":
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("INVALID_TIMEZONE") from exc
        if len(set(self.weekdays)) != len(self.weekdays) or any(
            d not in range(1, 8) for d in self.weekdays
        ):
            raise ValueError("INVALID_WEEKDAYS")
        if len(set(self.recipients)) != len(self.recipients):
            raise ValueError("DUPLICATE_RECIPIENT")
        return self


class DailySummarySchedule(ScheduleCommand):
    creator_membership_id: UUID | None = None
    id: UUID
    version: int = Field(ge=1)
    paused: bool
    effective_at: datetime | None = None


class ScheduleDraft(Contract):
    id: UUID
    command: ScheduleCommand
    expected_version: int = Field(ge=0)
    expires_at: datetime
    effective_at: datetime


class ConfirmSchedule(Contract):
    draft_id: UUID
    expected_version: int = Field(ge=0)


class PauseSchedule(Contract):
    expected_version: int = Field(ge=1)
    paused: bool


class Reporter(Contract):
    membership_id: UUID
    name: str


class ReportingWindow(Contract):
    id: UUID
    schedule_id: UUID
    applied_version: int
    local_date: str
    timezone: str
    starts_at: datetime
    cutoff_at: datetime
    ends_at: datetime
    expected_reporters: tuple[UUID, ...] = ()
    reported_members: tuple[UUID, ...] = ()
    coverage_state: Literal["NO_REPORTERS", "PARTIAL", "COMPLETE"] = "NO_REPORTERS"
    full_coverage: bool = False
    enabled: bool
    roster_observed_at: datetime | None = None
    scope_changed: bool = False


class ScheduleChanged(Contract):
    schedule_id: UUID
    version: StrictInt = Field(ge=1)
    paused: StrictBool


class ChatScheduleCommand(Contract):
    narrative_mode: Literal["NONE", "DRAFT_FOR_MANAGER"] | None = None
    narrative_locale: Literal["vi", "en"] | None = None
    project_reference: str = Field(min_length=1, max_length=200)
    operation: Literal["CONFIGURE", "PAUSE", "RESUME"]
    timezone: str | None = None
    cutoff: str | None = None
    weekdays: tuple[int, ...] | None = None
    recipient_references: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=100)
    send_when_complete: bool | None = None
    partial_at_cutoff: bool | None = None
