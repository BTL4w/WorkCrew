"""Typed report intents; tenant and role are never caller supplied."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from .metrics import ReportContract
from .periods import ReportKind, ReportPeriod


class CreateReportCommand(ReportContract):
    project_id: UUID
    kind: ReportKind = ReportKind.DAILY
    period_start: date | None = None
    timezone: str | None = None
    locale: Literal["vi", "en"] = "vi"
    narrative_enabled: bool = True


class CaptureReportCommand(ReportContract):
    report_id: UUID
    snapshot_id: UUID
    project_id: UUID
    period: ReportPeriod
    captured_at: datetime
