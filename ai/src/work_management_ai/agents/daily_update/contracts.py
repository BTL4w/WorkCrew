"""Tenant authority and versions come from the handoff, never model output."""

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DailyContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceRef(DailyContract):
    evidence_id: UUID
    version: int = Field(ge=1)


class DailyUpdateHandoff(DailyContract):
    text: str = Field(min_length=1, max_length=8000)
    locale: Literal["vi", "en"]
    task_id: UUID
    task_version: int = Field(ge=1)
    evidence_refs: tuple[EvidenceRef, ...] = Field(default=(), max_length=10)
    draft_id: UUID | None = None
    draft_version: int | None = Field(default=None, ge=1)


class BlockerDraft(DailyContract):
    text: str = Field(min_length=1, max_length=4000)
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]


class ExtractedReport(DailyContract):
    blockers: tuple[BlockerDraft, ...] = Field(default=(), max_length=20)
    reported_percent: Decimal | None = Field(ge=0, le=100, decimal_places=4)
    remaining_hours: Decimal | None = Field(ge=0, le=10000, decimal_places=4)
    spent_hours: Decimal | None = Field(ge=0, le=24, decimal_places=4)
    done_text: str = Field(max_length=4000)
    next_steps: str = Field(max_length=4000)
    needs_clarification: bool


class ReportingSnapshot(DailyContract):
    task_id: UUID
    task_version: int = Field(ge=1)
    progress_version: int = Field(ge=0)
    reporting_date: date
    evidence_refs: tuple[EvidenceRef, ...] = ()


class DailyUpdateResult(DailyContract):
    draft_id: UUID
    draft_version: int = Field(ge=1)
    task_id: UUID
    task_version: int = Field(ge=1)
    assessment_id: UUID | None
    needs_owner_confirmation: Literal[True] = True
