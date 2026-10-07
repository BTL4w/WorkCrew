"""Report identity and immutable metrics-only versions."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from app.modules.feedback.domain.feedback import Feedback
from app.modules.feedback.domain.outcomes import FeedbackOutcome, ReviewRates

from .generation import GenerationState
from .metrics import AggregateReceipt, ReportContract, SourceRef
from .narrative import NarrativeDocument
from .periods import ReportKind
from .snapshots import ReportMetricSnapshot


class ReportError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


class ReportCaptureConflict(Exception):
    """Retry the entire consistent transaction after serialization contention."""


class Report(ReportContract):
    origin: Literal["ON_DEMAND", "DAILY_SUMMARY"] = "ON_DEMAND"
    summary_id: UUID | None = None
    summary_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    workflow_version: str | None = None
    id: UUID
    organization_id: UUID
    project_id: UUID
    kind: ReportKind
    locale: Literal["vi", "en"]
    version: int = Field(ge=1)
    snapshot_id: UUID
    selected_version_id: UUID
    current_publication_id: UUID | None = None
    created_by_membership_id: UUID
    narrative_requested: bool
    created_at: datetime

    @model_validator(mode="after")
    def valid_summary_provenance(self) -> "Report":
        values = (self.summary_id, self.summary_hash, self.workflow_version)
        if (self.origin == "DAILY_SUMMARY" and any(v is None for v in values)) or (
            self.origin == "ON_DEMAND" and any(v is not None for v in values)
        ):
            raise ValueError("INVALID_SUMMARY_PROVENANCE")
        return self


class ReportVersion(ReportContract):
    id: UUID
    report_id: UUID
    snapshot_id: UUID
    origin: Literal["METRICS_ONLY", "AI_PROPOSED", "AI_EDITED"] = "METRICS_ONLY"
    locale: Literal["vi", "en"]
    created_at: datetime

    narrative: NarrativeDocument | None = None
    block_origins: dict[str, Literal["AI", "HUMAN"]] = Field(default_factory=dict)
    narrative_access_state: Literal["AVAILABLE", "UNAVAILABLE"] = "AVAILABLE"
    rendered_facts: dict[str, str] = Field(default_factory=dict)
    provenance: dict[str, JsonValue] = Field(default_factory=dict)
    generation_id: UUID | None = None
    base_version_id: UUID | None = None


class ReportPublication(ReportContract):
    id: UUID
    report_id: UUID
    report_version_id: UUID
    snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    publisher_membership_id: UUID
    decision_id: UUID
    published_at: datetime


class ReportResult(ReportContract):
    feedback: tuple[Feedback, ...] = ()
    feedback_outcomes: tuple[FeedbackOutcome, ...] = ()
    review_rates: ReviewRates | None = None
    report: Report
    snapshot: ReportMetricSnapshot
    selected_version: ReportVersion
    publications: tuple[ReportPublication, ...] = ()
    generation_state: GenerationState
    generation_id: UUID | None = None
    metrics_version_id: UUID | None = None
    verification_state: Literal["NOT_APPLICABLE", "PENDING", "VERIFIED", "FAILED"] = (
        "NOT_APPLICABLE"
    )
    review_state: Literal["PENDING", "ACCEPTED", "REJECTED"] = "PENDING"
    narrative_access_state: Literal["AVAILABLE", "UNAVAILABLE"] = "AVAILABLE"
    published_versions: tuple[ReportVersion, ...] = ()
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


class ReviewResult(ReportContract):
    report_result: ReportResult
    decision_id: UUID
    terminal_outcome_id: UUID
