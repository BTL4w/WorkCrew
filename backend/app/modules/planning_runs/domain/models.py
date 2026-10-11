"""Domain models and lifecycle state machines for AI planning runs persistence."""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class PlanningRunDomainError(Exception):
    """Base domain exception for planning runs."""


class InvalidTransitionError(PlanningRunDomainError):
    """Raised when an invalid state transition is attempted."""


class PlanningRunForbiddenError(PlanningRunDomainError):
    """Actor lacks permission for the planning-run operation."""


class PlanningRunNotFoundError(PlanningRunDomainError):
    """Run or proposal is absent or intentionally undisclosed."""


class WorkflowRunStateError(PlanningRunDomainError):
    """Run is not at the checkpoint required by the requested command."""


class UnsupportedPlanningCapabilityError(PlanningRunDomainError):
    """The request belongs to a capability outside Phase 2 planning."""


class IdempotencyKeyReusedError(PlanningRunDomainError):
    """An idempotency key was reused with a different normalized request."""


class ResourceVersionMismatchError(PlanningRunDomainError):
    """A stale proposal version was supplied."""

    def __init__(self, current_version: int) -> None:
        super().__init__("resource version mismatch")
        self.current_version = current_version


class ApprovalStateConflictError(PlanningRunDomainError):
    """Approval or Proposal is no longer in the required decision state."""


class ProposalStaleError(PlanningRunDomainError):
    """A proposal source reference changed after the immutable snapshot."""


class ProposalValidationError(PlanningRunDomainError):
    """Deterministic proposal validation blocks approval application."""


class WorkflowRunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    NEEDS_INPUT = "NEEDS_INPUT"
    DRAFT_AVAILABLE = "DRAFT_AVAILABLE"
    WAITING_FOR_DECISION = "WAITING_FOR_DECISION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in (
            WorkflowRunStatus.COMPLETED,
            WorkflowRunStatus.FAILED,
        )


class ProposalStatus(StrEnum):
    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    READY_FOR_DECISION = "READY_FOR_DECISION"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    STALE = "STALE"

    @property
    def is_terminal(self) -> bool:
        return self in (
            ProposalStatus.APPROVED,
            ProposalStatus.REJECTED,
        )


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"

    @property
    def is_terminal(self) -> bool:
        return self in (
            ApprovalStatus.APPROVED,
            ApprovalStatus.REJECTED,
            ApprovalStatus.SUPERSEDED,
        )


class WorkflowJobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    DISPATCHING = "DISPATCHING"
    DISPATCHED = "DISPATCHED"
    FAILED = "FAILED"


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _default_validation_result() -> dict[str, Any]:
    return {"status": "UNKNOWN", "is_valid": None, "errors": [], "warnings": []}


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    id: UUID
    organization_id: UUID
    project_id: UUID | None
    requested_by_membership_id: UUID
    status: WorkflowRunStatus
    workflow_name: str
    workflow_version: str
    verifier_version: str
    input_goal_text: str
    error_message: str | None = None
    version: int = 1
    created_at: datetime = field(default_factory=_now_utc)
    updated_at: datetime = field(default_factory=_now_utc)

    @classmethod
    def create(
        cls,
        *,
        id: UUID | None = None,
        organization_id: UUID,
        project_id: UUID | None,
        requested_by_membership_id: UUID,
        workflow_name: str,
        workflow_version: str,
        verifier_version: str,
        input_goal_text: str,
        created_at: datetime | None = None,
    ) -> "WorkflowRun":
        now = created_at or datetime.now(UTC)
        normalized_verifier_version = verifier_version.strip()
        if not normalized_verifier_version:
            raise PlanningRunDomainError("verifier_version must not be empty.")
        return cls(
            id=id or uuid4(),
            organization_id=organization_id,
            project_id=project_id,
            requested_by_membership_id=requested_by_membership_id,
            status=WorkflowRunStatus.QUEUED,
            workflow_name=workflow_name,
            workflow_version=workflow_version,
            verifier_version=normalized_verifier_version,
            input_goal_text=input_goal_text.strip(),
            error_message=None,
            version=1,
            created_at=now,
            updated_at=now,
        )

    def mark_running(self, now: datetime | None = None) -> "WorkflowRun":
        if self.status not in (
            WorkflowRunStatus.QUEUED,
            WorkflowRunStatus.NEEDS_INPUT,
            WorkflowRunStatus.DRAFT_AVAILABLE,
            WorkflowRunStatus.WAITING_FOR_DECISION,
        ):
            raise InvalidTransitionError(
                f"Cannot transition WorkflowRun from {self.status} to RUNNING."
            )
        current_time = now or datetime.now(UTC)
        return replace(
            self,
            status=WorkflowRunStatus.RUNNING,
            version=self.version + 1,
            updated_at=current_time,
        )

    def mark_draft_available(self, now: datetime | None = None) -> "WorkflowRun":
        if self.status is not WorkflowRunStatus.RUNNING:
            raise InvalidTransitionError("Draft availability requires a running workflow.")
        return replace(
            self,
            status=WorkflowRunStatus.DRAFT_AVAILABLE,
            version=self.version + 1,
            updated_at=now or datetime.now(UTC),
        )

    def mark_needs_input(self, now: datetime | None = None) -> "WorkflowRun":
        if self.status != WorkflowRunStatus.RUNNING:
            raise InvalidTransitionError(
                f"Cannot transition WorkflowRun from {self.status} to NEEDS_INPUT."
            )
        current_time = now or datetime.now(UTC)
        return replace(
            self,
            status=WorkflowRunStatus.NEEDS_INPUT,
            version=self.version + 1,
            updated_at=current_time,
        )

    def mark_waiting_for_decision(self, now: datetime | None = None) -> "WorkflowRun":
        if self.status != WorkflowRunStatus.RUNNING:
            raise InvalidTransitionError(
                f"Cannot transition WorkflowRun from {self.status} to WAITING_FOR_DECISION."
            )
        current_time = now or datetime.now(UTC)
        return replace(
            self,
            status=WorkflowRunStatus.WAITING_FOR_DECISION,
            version=self.version + 1,
            updated_at=current_time,
        )

    def mark_completed(self, now: datetime | None = None) -> "WorkflowRun":
        if self.status not in (WorkflowRunStatus.RUNNING, WorkflowRunStatus.WAITING_FOR_DECISION):
            raise InvalidTransitionError(
                f"Cannot transition WorkflowRun from {self.status} to COMPLETED."
            )
        current_time = now or datetime.now(UTC)
        return replace(
            self,
            status=WorkflowRunStatus.COMPLETED,
            version=self.version + 1,
            updated_at=current_time,
        )

    def mark_failed(self, error_message: str, now: datetime | None = None) -> "WorkflowRun":
        if self.status.is_terminal:
            raise InvalidTransitionError(
                f"Cannot transition WorkflowRun from terminal status {self.status} to FAILED."
            )
        current_time = now or datetime.now(UTC)
        return replace(
            self,
            status=WorkflowRunStatus.FAILED,
            error_message=error_message,
            version=self.version + 1,
            updated_at=current_time,
        )


@dataclass(frozen=True, slots=True)
class WorkflowCheckpoint:
    id: UUID
    organization_id: UUID
    workflow_run_id: UUID
    node: str
    sequence: int
    state: dict[str, Any]
    created_at: datetime = field(default_factory=_now_utc)


def _default_field_provenance() -> dict[str, Any]:
    return {}


def _default_source_reference_snapshot() -> list[dict[str, Any]]:
    return []


@dataclass(frozen=True, slots=True)
class ProposalVersion:
    id: UUID
    organization_id: UUID
    proposal_id: UUID
    version_number: int
    created_by_membership_id: UUID
    content: dict[str, Any]
    assumptions: list[dict[str, Any]]
    change_summary: str | None = None
    field_provenance: dict[str, Any] = field(
        default_factory=_default_field_provenance,
    )
    validation_result: dict[str, Any] = field(
        default_factory=_default_validation_result,
    )
    source_reference_snapshot: list[dict[str, Any]] = field(
        default_factory=_default_source_reference_snapshot,
    )
    workflow_version: str = "UNKNOWN"
    prompt_version: str = "UNKNOWN"
    schema_version: str = "UNKNOWN"
    model_reference: str = "UNKNOWN"
    verifier_version: str = "UNKNOWN"
    source_draft_id: UUID | None = None
    source_draft_version: int | None = None
    creator_type: str = "UNKNOWN"
    created_at: datetime = field(default_factory=_now_utc)

    def __post_init__(self) -> None:
        if self.creator_type not in (
            "AI_SYSTEM",
            "HUMAN_MANAGER",
            "UNKNOWN",
        ):
            raise PlanningRunDomainError(
                f"Invalid creator_type '{self.creator_type}'. "
                "Must be AI_SYSTEM, HUMAN_MANAGER, or UNKNOWN."
            )


@dataclass(frozen=True, slots=True)
class Proposal:
    id: UUID
    organization_id: UUID
    workflow_run_id: UUID
    status: ProposalStatus
    current_version_number: int
    approval_id: UUID | None = None
    superseded_approval_id: UUID | None = None
    version: int = 1
    created_at: datetime = field(default_factory=_now_utc)
    updated_at: datetime = field(default_factory=_now_utc)

    @classmethod
    def create(
        cls,
        *,
        id: UUID | None = None,
        organization_id: UUID,
        workflow_run_id: UUID,
        current_version_number: int = 1,
        created_at: datetime | None = None,
    ) -> "Proposal":
        now = created_at or datetime.now(UTC)
        return cls(
            id=id or uuid4(),
            organization_id=organization_id,
            workflow_run_id=workflow_run_id,
            status=ProposalStatus.DRAFT,
            current_version_number=current_version_number,
            approval_id=None,
            superseded_approval_id=None,
            version=1,
            created_at=now,
            updated_at=now,
        )

    def mark_validating(self, now: datetime | None = None) -> "Proposal":
        if self.status != ProposalStatus.DRAFT:
            raise InvalidTransitionError(
                f"Cannot transition Proposal from {self.status} to VALIDATING."
            )
        current_time = now or datetime.now(UTC)
        return replace(
            self,
            status=ProposalStatus.VALIDATING,
            version=self.version + 1,
            updated_at=current_time,
        )

    def mark_ready_for_decision(
        self,
        approval_id: UUID,
        now: datetime | None = None,
    ) -> "Proposal":
        allowed = (
            ProposalStatus.DRAFT,
            ProposalStatus.VALIDATING,
            ProposalStatus.READY_FOR_DECISION,
        )
        if self.status not in allowed:
            raise InvalidTransitionError(
                f"Cannot transition Proposal from {self.status} to READY_FOR_DECISION."
            )
        current_time = now or datetime.now(UTC)
        is_re_ready = (
            self.status == ProposalStatus.READY_FOR_DECISION and self.approval_id != approval_id
        )
        superseded = self.approval_id if is_re_ready else self.superseded_approval_id
        return replace(
            self,
            status=ProposalStatus.READY_FOR_DECISION,
            approval_id=approval_id,
            superseded_approval_id=superseded,
            version=self.version + 1,
            updated_at=current_time,
        )

    def edit(self, now: datetime | None = None) -> "Proposal":
        if self.status.is_terminal:
            raise InvalidTransitionError(f"Cannot edit Proposal in terminal status {self.status}.")
        current_time = now or datetime.now(UTC)
        is_ready = self.status == ProposalStatus.READY_FOR_DECISION
        superseded = self.approval_id if is_ready else self.superseded_approval_id
        return replace(
            self,
            status=ProposalStatus.DRAFT,
            current_version_number=self.current_version_number + 1,
            approval_id=None,
            superseded_approval_id=superseded,
            version=self.version + 1,
            updated_at=current_time,
        )

    def mark_stale(self, now: datetime | None = None) -> "Proposal":
        if self.status.is_terminal:
            raise InvalidTransitionError(
                f"Cannot mark Proposal as STALE from terminal status {self.status}."
            )
        current_time = now or datetime.now(UTC)
        is_ready = self.status == ProposalStatus.READY_FOR_DECISION
        superseded = self.approval_id if is_ready else self.superseded_approval_id
        return replace(
            self,
            status=ProposalStatus.STALE,
            approval_id=None,
            superseded_approval_id=superseded,
            version=self.version + 1,
            updated_at=current_time,
        )

    def mark_approved(self, now: datetime | None = None) -> "Proposal":
        if self.status != ProposalStatus.READY_FOR_DECISION:
            raise InvalidTransitionError(f"Cannot approve Proposal from status {self.status}.")
        current_time = now or datetime.now(UTC)
        return replace(
            self,
            status=ProposalStatus.APPROVED,
            version=self.version + 1,
            updated_at=current_time,
        )

    def mark_rejected(self, now: datetime | None = None) -> "Proposal":
        if self.status != ProposalStatus.READY_FOR_DECISION:
            raise InvalidTransitionError(f"Cannot reject Proposal from status {self.status}.")
        current_time = now or datetime.now(UTC)
        return replace(
            self,
            status=ProposalStatus.REJECTED,
            version=self.version + 1,
            updated_at=current_time,
        )


@dataclass(frozen=True, slots=True)
class Approval:
    id: UUID
    organization_id: UUID
    proposal_id: UUID
    proposal_version_number: int
    status: ApprovalStatus
    decided_by_membership_id: UUID | None = None
    decision_reason: str | None = None
    decided_at: datetime | None = None
    version: int = 1
    created_at: datetime = field(default_factory=_now_utc)
    updated_at: datetime = field(default_factory=_now_utc)

    @classmethod
    def create(
        cls,
        *,
        id: UUID | None = None,
        organization_id: UUID,
        proposal_id: UUID,
        proposal_version_number: int,
        created_at: datetime | None = None,
    ) -> "Approval":
        now = created_at or datetime.now(UTC)
        return cls(
            id=id or uuid4(),
            organization_id=organization_id,
            proposal_id=proposal_id,
            proposal_version_number=proposal_version_number,
            status=ApprovalStatus.PENDING,
            decided_by_membership_id=None,
            decision_reason=None,
            decided_at=None,
            version=1,
            created_at=now,
            updated_at=now,
        )

    def decide_approve(
        self,
        *,
        decided_by: UUID,
        decision_reason: str | None = None,
        decided_at: datetime | None = None,
    ) -> "Approval":
        if self.status != ApprovalStatus.PENDING:
            raise InvalidTransitionError(f"Cannot decide Approval from status {self.status}.")
        now = decided_at or datetime.now(UTC)
        return replace(
            self,
            status=ApprovalStatus.APPROVED,
            decided_by_membership_id=decided_by,
            decision_reason=decision_reason,
            decided_at=now,
            version=self.version + 1,
            updated_at=now,
        )

    def decide_reject(
        self,
        *,
        decided_by: UUID,
        decision_reason: str | None = None,
        decided_at: datetime | None = None,
    ) -> "Approval":
        if self.status != ApprovalStatus.PENDING:
            raise InvalidTransitionError(f"Cannot decide Approval from status {self.status}.")
        now = decided_at or datetime.now(UTC)
        return replace(
            self,
            status=ApprovalStatus.REJECTED,
            decided_by_membership_id=decided_by,
            decision_reason=decision_reason,
            decided_at=now,
            version=self.version + 1,
            updated_at=now,
        )

    def mark_superseded(self, now: datetime | None = None) -> "Approval":
        if self.status != ApprovalStatus.PENDING:
            raise InvalidTransitionError(
                f"Cannot mark Approval as superseded from status {self.status}."
            )
        current_time = now or datetime.now(UTC)
        return replace(
            self,
            status=ApprovalStatus.SUPERSEDED,
            version=self.version + 1,
            updated_at=current_time,
        )


@dataclass(frozen=True, slots=True)
class WorkflowJob:
    id: UUID
    organization_id: UUID
    workflow_run_id: UUID
    job_type: str
    status: WorkflowJobStatus
    payload: dict[str, Any]
    attempt_count: int = 0
    max_attempts: int = 3
    available_at: datetime = field(default_factory=_now_utc)
    locked_by_worker_id: str | None = None
    lease_until: datetime | None = None
    last_error: str | None = None
    created_at: datetime = field(default_factory=_now_utc)
    updated_at: datetime = field(default_factory=_now_utc)


@dataclass(frozen=True, slots=True)
class WorkflowEvent:
    id: UUID
    organization_id: UUID
    workflow_run_id: UUID
    sequence: int
    event_type: str
    public_payload: dict[str, Any]
    created_at: datetime = field(default_factory=_now_utc)


@dataclass(frozen=True, slots=True)
class ModelInvocation:
    id: UUID
    organization_id: UUID
    workflow_run_id: UUID
    provider: str
    model_name: str
    prompt_version: str
    schema_version: str
    invocation_key: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    duration_ms: int | None = None
    status: str = "SUCCESS"
    created_at: datetime = field(default_factory=_now_utc)


@dataclass(frozen=True, slots=True)
class ContextReference:
    id: UUID
    organization_id: UUID
    workflow_run_id: UUID
    resource_type: str
    resource_id: UUID
    provenance_notes: str | None = None
    created_at: datetime = field(default_factory=_now_utc)


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    id: UUID
    organization_id: UUID
    event_id: UUID
    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    payload: dict[str, Any]
    status: OutboxStatus = OutboxStatus.PENDING
    envelope_version: str = "1.0"
    attempt_count: int = 0
    max_attempts: int = 3
    available_at: datetime = field(default_factory=_now_utc)
    published_at: datetime | None = None
    last_error_code: str | None = None
    last_error: str | None = None
    locked_by_worker_id: str | None = None
    lease_until: datetime | None = None
    occurred_at: datetime = field(default_factory=_now_utc)
    created_at: datetime = field(default_factory=_now_utc)
