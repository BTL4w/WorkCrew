"""Immutable factual summaries and authenticated worker scope; no model output."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.modules.identity.domain.auth import AuthenticatedActor

from .schedules import Contract, Reporter, ReportingWindow


@dataclass(frozen=True)
class AuthorizedJobScope:
    actor: AuthenticatedActor
    at: datetime


class SummaryTask(Contract):
    id: UUID
    title: str
    task_version: int
    assignee_id: UUID | None
    status: str
    observation_id: UUID | None = None
    progress_version: int = 0
    reported_percent: str | None = None
    remaining_hours: str | None = None
    observed_at: datetime | None = None


class SummarySource(Contract):
    id: UUID
    task_id: UUID
    version: int
    kind: Literal["BLOCKER", "RISK", "REVIEW", "EVIDENCE"]
    text: str | None = None
    state: str | None = None
    created_at: datetime | None = None
    evidence_id: UUID | None = None
    evidence_version: int | None = None
    href: str | None = None


class DailySummarySnapshot(Contract):
    id: UUID
    schedule_id: UUID
    project_id: UUID
    project_name: str
    window: ReportingWindow
    reason: Literal["COVERAGE", "CUTOFF", "RESUME"]
    snapshot_at: datetime
    scope: Literal["PROJECT", "OWN_WORK"] = "PROJECT"
    expected_count: int = Field(ge=0)
    reported_count: int = Field(ge=0)
    missing_reporters: tuple[UUID, ...]
    reporters: tuple[Reporter, ...] = ()
    tasks: tuple[SummaryTask, ...]
    sources: tuple[SummarySource, ...]
    unknown_inputs: tuple[str, ...] = ()


class SummaryDelivery(Contract):
    id: UUID
    snapshot: DailySummarySnapshot


class SummaryCaptured(Contract):
    summary_id: UUID


class SummaryDelivered(Contract):
    delivery_id: UUID
    recipient_id: UUID
