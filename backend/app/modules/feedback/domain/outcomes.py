"""Verified outcome references and generation-based quality rates, without causality claims."""

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, JsonValue

from app.modules.reporting.domain.metrics import ReportContract

from .feedback import Feedback


class OutcomeSourceCommand(ReportContract):
    source_type: Literal["TASK_TRANSITION", "TASK_ACTUALS", "BLOCKER_RESOLUTION"]
    source_id: UUID
    source_version: int = Field(ge=0)


class OutcomeFacts(ReportContract):
    project_id: UUID
    source: OutcomeSourceCommand
    state: Literal["AVAILABLE", "UNKNOWN"]
    facts: dict[str, JsonValue]
    occurred_at: datetime | None
    observation_id: UUID | None = None


class FeedbackOutcome(ReportContract):
    id: UUID
    feedback_id: UUID
    actor_membership_id: UUID
    schema_version: Literal["feedback-outcome.v1"] = "feedback-outcome.v1"
    source: OutcomeSourceCommand
    state: Literal["AVAILABLE", "UNKNOWN"]
    facts: dict[str, JsonValue]
    occurred_at: datetime | None
    recorded_at: datetime


class ReviewRates(ReportContract):
    reviewed_generation_count: int
    accept_count: int
    edit_count: int
    reject_count: int
    accept_percent: Decimal | None
    edit_percent: Decimal | None
    reject_percent: Decimal | None
    pending_generation_count: int = 0
    failed_generation_count: int = 0
    manual_report_count: int = 0


def review_rates(
    rows: Iterable[Feedback], *, pending: int = 0, failed: int = 0, manual: int = 0
) -> ReviewRates:
    terminal = {row.generation_id: row.decision for row in rows if row.kind == "TERMINAL_QUALITY"}
    n = len(terminal)
    counts = {
        decision: sum(value == decision for value in terminal.values())
        for decision in ("ACCEPT", "EDIT", "REJECT")
    }
    return ReviewRates(
        reviewed_generation_count=n,
        accept_count=counts["ACCEPT"],
        edit_count=counts["EDIT"],
        reject_count=counts["REJECT"],
        accept_percent=Decimal(counts["ACCEPT"]) * 100 / n if n else None,
        edit_percent=Decimal(counts["EDIT"]) * 100 / n if n else None,
        reject_percent=Decimal(counts["REJECT"]) * 100 / n if n else None,
        pending_generation_count=pending,
        failed_generation_count=failed,
        manual_report_count=manual,
    )
