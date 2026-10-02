"""Allowlisted draft preparation operations; confirmation is deliberately absent."""

from typing import Literal
from uuid import UUID

from pydantic import Field, RootModel

from work_management_ai.agents.daily_update.contracts import (
    DailyContract,
    DailyUpdateResult,
    EvidenceRef,
    ExtractedReport,
    ReportingSnapshot,
)


class DailyUpdateToolInput(DailyContract):
    action: Literal["CONTEXT", "DRAFT", "ASSESS"]
    task_id: UUID
    task_version: int = Field(ge=1)
    evidence_refs: tuple[EvidenceRef, ...] = Field(default=(), max_length=10)
    draft_id: UUID | None = None
    draft_version: int | None = Field(default=None, ge=1)
    report: ExtractedReport | None = None


class DailyUpdateToolOutput(RootModel[ReportingSnapshot | DailyUpdateResult]):
    pass
