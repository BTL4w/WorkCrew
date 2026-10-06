"""Human feedback with server-resolved immutable AI lineage."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, JsonValue

from app.modules.reporting.domain.metrics import ReportContract


class FeedbackCommand(ReportContract):
    report_id: UUID
    report_version_id: UUID
    decision: Literal["ACCEPT", "EDIT", "REJECT"]
    reason: str = Field(min_length=1, max_length=2000)


class Feedback(ReportContract):
    id: UUID
    report_id: UUID
    report_version_id: UUID
    original_version_id: UUID
    generation_id: UUID
    actor_membership_id: UUID
    decision_id: UUID | None = None
    kind: Literal["TERMINAL_QUALITY", "ADVISORY"]
    decision: Literal["ACCEPT", "EDIT", "REJECT"]
    reason: str | None
    provenance: dict[str, JsonValue]
    created_at: datetime


class FeedbackResult(ReportContract):
    feedback: Feedback
    replayed: bool = False


class TerminalReviewCommand(FeedbackCommand):
    original_version_id: UUID
    generation_id: UUID
    decision_id: UUID
    provenance: dict[str, JsonValue]
