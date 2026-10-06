"""Durable generation identity and state, never human approval."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from .metrics import ReportContract

type GenerationState = Literal[
    "NOT_REQUESTED", "QUEUED", "RUNNING", "AWAITING_REVIEW", "AI_UNAVAILABLE", "FAILED"
]


class GenerationJob(ReportContract):
    id: UUID
    organization_id: UUID
    report_id: UUID
    base_version_id: UUID
    snapshot_id: UUID
    snapshot_hash: str
    requester_membership_id: UUID
    request_key: str
    expected_report_version: int
    state: GenerationState
    claims: int
    fence: int
    lease_owner: str | None
    lease_until: datetime | None
    started_at: datetime | None
    deadline: datetime | None
    orchestration_run_id: UUID | None
    proposed_version_id: UUID | None
    job_type: Literal["DRAFT", "EDIT_VERIFICATION"] = "DRAFT"
    original_generation_id: UUID | None = None
    safe_error_code: str | None
    created_at: datetime
