"""Report identity and immutable metrics-only versions."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from .metrics import AggregateReceipt, ReportContract, SourceRef
from .periods import ReportKind
from .snapshots import ReportMetricSnapshot


class ReportError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


class ReportCaptureConflict(Exception):
    """Retry the entire consistent transaction after serialization contention."""


class Report(ReportContract):
    id: UUID
    organization_id: UUID
    project_id: UUID
    kind: ReportKind
    locale: Literal["vi", "en"]
    version: int = Field(ge=1)
    snapshot_id: UUID
    selected_version_id: UUID
    created_by_membership_id: UUID
    narrative_requested: bool
    created_at: datetime


class ReportVersion(ReportContract):
    id: UUID
    report_id: UUID
    snapshot_id: UUID
    origin: Literal["METRICS_ONLY"] = "METRICS_ONLY"
    locale: Literal["vi", "en"]
    created_at: datetime


class ReportResult(ReportContract):
    report: Report
    snapshot: ReportMetricSnapshot
    selected_version: ReportVersion
    publications: tuple[()] = ()
    generation_state: Literal["NOT_REQUESTED", "AI_UNAVAILABLE"]
    replayed: bool = False


class ReportPage(ReportContract):
    items: tuple[Report, ...]
    page: int
    page_size: int
    total: int


class ReportDefaults(ReportContract):
    timezone: str
    period_start: date


class ReportSourceItem(ReportContract):
    source: SourceRef
    freshness: Literal["CURRENT", "UPDATED", "UNAVAILABLE"]


class ReportSourcePage(ReportContract):
    snapshot_hash: str
    items: tuple[ReportSourceItem, ...]
    receipts: tuple[AggregateReceipt, ...]
    next_cursor: str | None
    total: int
