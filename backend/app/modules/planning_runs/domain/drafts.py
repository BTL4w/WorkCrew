"""Backend-owned operational draft snapshots, separate from business plans."""

from dataclasses import dataclass, field
from datetime import date
from uuid import UUID


@dataclass(frozen=True, slots=True)
class DraftContent:
    content: dict[str, object]
    question: dict[str, object] | None = None
    validation: dict[str, object] = field(default_factory=lambda: dict[str, object]())
    versions: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    change_summary: str | None = None


@dataclass(frozen=True, slots=True)
class PlanningDraftSnapshot:
    draft_id: UUID
    workflow_run_id: UUID
    draft_version: int
    current_version: int
    fingerprint: str
    content: DraftContent
    can_prepare: bool = True
    suggested_anchor_date: date | None = None


@dataclass(frozen=True, slots=True)
class PreparedPlanningAction:
    workflow_run_id: UUID
    proposal_id: UUID
    proposal_version: int
    approval_id: UUID
    draft_id: UUID
    draft_version: int
    fingerprint: str
