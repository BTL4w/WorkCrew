"""Typed manual observations, independent of Task status and planned effort."""

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import Field

from app.modules.progress.domain.blockers import BlockerCommand
from app.modules.progress.domain.reporting_contracts import ReportingContract, SelectedEvidence


class DailyUpdateError(Exception):
    def __init__(self, code: str, status: int = 409, *, details: dict[str, object] | None = None):
        super().__init__(code)
        self.code, self.status = code, status
        self.details = details or {}


class DailyUpdateItemInput(ReportingContract):
    task_id: UUID
    expected_task_version: int = Field(ge=1)
    expected_progress_version: int = Field(ge=0)
    reported_percent: Decimal = Field(ge=0, le=100, max_digits=7, decimal_places=4)
    remaining_hours: Decimal | None = Field(default=None, ge=0, le=10000, decimal_places=4)
    spent_hours: Decimal | None = Field(default=None, ge=0, le=24, decimal_places=4)
    reporting_date: date
    done_text: str = Field(min_length=1, max_length=4000)
    next_steps: str = Field(default="", max_length=4000)
    evidence_refs: tuple[SelectedEvidence, ...] = Field(default=(), max_length=20)
    blocker_commands: tuple[BlockerCommand, ...] = Field(default=(), max_length=20)
    corrects_observation_id: UUID | None = None
    correction_reason: str = Field(default="", max_length=1000)


class DailyUpdateDraft(ReportingContract):
    id: UUID
    version: int
    content_hash: str
    items: tuple[DailyUpdateItemInput, ...]
    assessment_state: Literal["UNAVAILABLE", "PENDING", "READY", "NOT_ASSESSED_NO_EVIDENCE"] = (
        "UNAVAILABLE"
    )
    reporting_timezone: str
    confirmed_update_id: UUID | None = None


class ConfirmDailyUpdateCommand(ReportingContract):
    draft_id: UUID
    expected_draft_version: int = Field(ge=1)
    assessment_id: UUID | None = None
    warning_acknowledgments: tuple[UUID, ...] = ()
    # Versions are bound to the immutable draft content; cannot be replaced on confirmation.


class ConfirmedObservation(ReportingContract):
    id: UUID
    update_id: UUID
    item: DailyUpdateItemInput
    progress_version: int
    reporting_at: datetime
    confirmed_at: datetime
    reporting_timezone: str
    late: bool
    project_week_state: Literal["LINKED", "NO_PROJECT_WEEK"]


class ConfirmedDailyUpdate(ReportingContract):
    id: UUID
    draft_id: UUID
    observations: tuple[ConfirmedObservation, ...]
    assessment_state: Literal["UNAVAILABLE", "PENDING", "READY", "NOT_ASSESSED_NO_EVIDENCE"] = (
        "UNAVAILABLE"
    )


class TaskReportingContext(ReportingContract):
    task_id: UUID
    task_version: int
    progress_version: int
    reported_percent: Decimal | None
    remaining_hours: Decimal | None
    reporting_timezone: str
    reporting_date: date
    project_week_state: Literal["LINKED", "NO_PROJECT_WEEK"]
    evidence_refs: tuple[SelectedEvidence, ...] = ()


def validate_items(items: tuple[DailyUpdateItemInput, ...], at: datetime, timezone: str) -> None:
    if not items or len(items) > 50:
        raise DailyUpdateError("INVALID_ITEMS", 422)
    if len({i.task_id for i in items}) != len(items):
        raise DailyUpdateError("DUPLICATE_TASK", 422)
    totals: defaultdict[date, Decimal] = defaultdict(lambda: Decimal(0))
    for item in items:
        if any(
            c.task_id != item.task_id or c.expected_task_version != item.expected_task_version
            for c in item.blocker_commands
        ):
            raise DailyUpdateError("INVALID_BLOCKER_TASK", 422)
        if item.reporting_date > at.astimezone(ZoneInfo(timezone)).date():
            raise DailyUpdateError("FUTURE_REPORT", 422)
        if item.reported_percent == 100 and not item.evidence_refs:
            raise DailyUpdateError("EVIDENCE_REQUIRED", 422)
        if item.corrects_observation_id and not item.correction_reason.strip():
            raise DailyUpdateError("CORRECTION_REASON_REQUIRED", 422)
        if len(set(item.evidence_refs)) != len(item.evidence_refs):
            raise DailyUpdateError("DUPLICATE_EVIDENCE", 422)
        totals[item.reporting_date] += item.spent_hours or Decimal(0)
    if any(total > 24 for total in totals.values()):
        raise DailyUpdateError("DAILY_HOURS_LIMIT", 422)
