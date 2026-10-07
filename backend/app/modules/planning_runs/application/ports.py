"""Application ports and transaction boundaries for planning runs."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.planning_runs.application.approval_ports import ApprovalDecisionResult
from app.modules.planning_runs.domain.models import (
    Approval,
    ContextReference,
    ModelInvocation,
    OutboxEvent,
    Proposal,
    ProposalVersion,
    WorkflowCheckpoint,
    WorkflowEvent,
    WorkflowJob,
    WorkflowRun,
)
from app.modules.planning_runs.domain.risk_replanning import RiskPlanBinding, RiskPlanMetadata


class PlanningRunRepository(Protocol):
    """Repository port for AI planning run entities and workflow state."""

    async def fail_risk_revision_run(self, *, organization_id: UUID, run_id: UUID) -> bool: ...

    async def authenticate_risk_plan_actor(self, *, actor: AuthenticatedActor) -> None: ...
    async def load_risk_plan(
        self, *, actor: AuthenticatedActor, binding: RiskPlanBinding
    ) -> tuple[RiskPlanMetadata, dict[str, object]]: ...
    async def verify_risk_plan(
        self, *, actor: AuthenticatedActor, metadata: RiskPlanMetadata
    ) -> None: ...
    async def verify_risk_plan_content(
        self, *, actor: AuthenticatedActor, content: dict[str, object]
    ) -> None: ...
    async def get_risk_revision_job(
        self, *, actor: AuthenticatedActor, run_id: UUID
    ) -> WorkflowJob | None: ...

    async def create_workflow_run(
        self,
        *,
        run: WorkflowRun,
        job: WorkflowJob | None = None,
    ) -> WorkflowRun: ...

    async def decide_approval_mutation(
        self,
        **values: object,
    ) -> ApprovalDecisionResult: ...

    async def mark_stale_decision_attempt(self, **values: object) -> None: ...

    async def create_planning_run_mutation(
        self,
        **values: object,
    ) -> "WorkflowRunMutationResult": ...

    async def list_workflow_runs(
        self, *, actor: AuthenticatedActor, limit: int
    ) -> tuple[WorkflowRun, ...]: ...

    async def resume_planning_run_mutation(
        self,
        **values: object,
    ) -> "WorkflowRunMutationResult": ...

    async def find_workflow_run_mutation_replay(
        self, **values: object
    ) -> "WorkflowRunMutationResult | None": ...

    async def edit_proposal_mutation(
        self,
        **values: object,
    ) -> "ProposalMutationResult": ...

    async def find_proposal_mutation_replay(
        self, **values: object
    ) -> "ProposalMutationResult | None": ...

    async def request_ai_revision_mutation(
        self, **values: object
    ) -> "ProposalRevisionRequestResult": ...

    async def get_ai_revision_preflight(
        self, **values: object
    ) -> "ProposalAIRevisionPreflight | None": ...

    async def finalize_ai_revision_mutation(self, **values: object) -> ProposalVersion | None: ...

    async def find_invalid_active_membership_ids(
        self,
        *,
        actor: AuthenticatedActor,
        membership_ids: set[UUID],
    ) -> set[UUID]: ...

    async def complete_proposal_revalidation(
        self,
        *,
        actor: AuthenticatedActor,
        proposal_id: UUID,
        version_number: int,
        validation_result: dict[str, object],
        request_id: str,
    ) -> Proposal: ...

    async def audit_rejection(
        self,
        *,
        actor: AuthenticatedActor,
        action: str,
        request_id: str,
        reason_code: str,
        idempotency_key: str | None = None,
        resource_id: UUID | None = None,
        **values: object,
    ) -> None: ...

    async def get_workflow_run(
        self,
        *,
        actor: AuthenticatedActor,
        run_id: UUID,
    ) -> WorkflowRun | None: ...

    async def assert_execution_context(self, *, organization_id: UUID, run_id: UUID) -> None: ...

    async def get_workflow_run_by_scope(
        self, *, organization_id: UUID, run_id: UUID
    ) -> WorkflowRun | None: ...

    async def list_active_membership_ids(self, *, organization_id: UUID) -> frozenset[UUID]: ...

    async def update_workflow_run(
        self,
        *,
        actor: AuthenticatedActor,
        run: WorkflowRun,
    ) -> WorkflowRun: ...

    async def save_checkpoint(
        self,
        *,
        checkpoint: WorkflowCheckpoint,
    ) -> WorkflowCheckpoint: ...

    async def get_latest_checkpoint(
        self,
        *,
        actor: AuthenticatedActor,
        run_id: UUID,
        include_state: bool = True,
    ) -> WorkflowCheckpoint | None: ...

    async def create_proposal(
        self,
        *,
        proposal: Proposal,
        initial_version: ProposalVersion,
    ) -> Proposal: ...

    async def get_proposal(
        self,
        *,
        actor: AuthenticatedActor,
        proposal_id: UUID,
    ) -> Proposal | None: ...

    async def get_proposal_by_run_id(
        self,
        *,
        actor: AuthenticatedActor,
        run_id: UUID,
    ) -> Proposal | None: ...

    async def update_proposal(
        self,
        *,
        actor: AuthenticatedActor,
        proposal: Proposal,
    ) -> Proposal: ...

    async def edit_proposal(
        self,
        *,
        actor: AuthenticatedActor,
        proposal: Proposal,
        version: ProposalVersion,
        superseded_approval: Approval,
    ) -> Proposal: ...

    async def append_proposal_version(
        self,
        *,
        version: ProposalVersion,
    ) -> ProposalVersion: ...

    async def get_proposal_version(
        self,
        *,
        actor: AuthenticatedActor,
        proposal_id: UUID,
        version_number: int,
    ) -> ProposalVersion | None: ...

    async def get_proposal_version_creator_by_scope(
        self,
        *,
        organization_id: UUID,
        proposal_id: UUID,
        version_number: int,
    ) -> UUID | None: ...

    async def create_approval(
        self,
        *,
        approval: Approval,
    ) -> Approval: ...

    async def get_approval(
        self,
        *,
        actor: AuthenticatedActor,
        approval_id: UUID,
    ) -> Approval | None: ...

    async def get_approval_decider_by_scope(
        self, *, organization_id: UUID, approval_id: UUID
    ) -> UUID | None: ...

    async def decide_approval(
        self,
        *,
        actor: AuthenticatedActor,
        approval: Approval,
        proposal: Proposal,
    ) -> Approval: ...

    async def append_event(
        self,
        *,
        event: WorkflowEvent | None = None,
        actor: AuthenticatedActor | None = None,
        run_id: UUID | None = None,
        event_type: str | None = None,
        public_payload: dict[str, Any] | None = None,
    ) -> WorkflowEvent: ...

    async def list_events(
        self,
        *,
        actor: AuthenticatedActor,
        run_id: UUID,
        after_sequence: int = 0,
    ) -> list[WorkflowEvent]: ...

    async def record_model_invocation(
        self,
        *,
        invocation: ModelInvocation,
    ) -> ModelInvocation: ...

    async def add_context_reference(
        self,
        *,
        ref: ContextReference,
    ) -> ContextReference: ...

    async def enqueue_outbox_event(
        self,
        *,
        event: OutboxEvent,
        organization_id: UUID,
    ) -> OutboxEvent: ...

    async def claim_pending_outbox_events(
        self,
        *,
        organization_id: UUID,
        worker_id: str,
        limit: int,
        now: datetime,
        lease_until: datetime,
    ) -> list[OutboxEvent]: ...

    async def mark_outbox_event_published(
        self,
        *,
        organization_id: UUID,
        event_id: UUID,
        worker_id: str,
        now: datetime,
        published_at: datetime,
    ) -> None: ...

    async def record_outbox_event_failure(
        self,
        *,
        organization_id: UUID,
        event_id: UUID,
        worker_id: str,
        now: datetime,
        error_code: str,
        error_message: str,
        next_available_at: datetime,
    ) -> None: ...

    async def claim_job(
        self,
        *,
        organization_id: UUID | None = None,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> WorkflowJob | None: ...

    async def complete_job(
        self,
        *,
        job_id: UUID,
        worker_id: str,
    ) -> None: ...

    async def fail_job(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        error_message: str,
        next_available_at: datetime,
    ) -> None: ...


class PlanningRunTransaction(Protocol):
    """Context manager for atomic planning run transaction boundaries."""

    @property
    def repository(self) -> PlanningRunRepository: ...

    @property
    def session(self) -> Any: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

    async def __aenter__(self) -> "PlanningRunTransaction": ...

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkflowRunMutationResult:
    run: WorkflowRun
    replayed: bool


@dataclass(frozen=True, slots=True)
class ProposalMutationResult:
    proposal: Proposal
    version: ProposalVersion
    replayed: bool


@dataclass(frozen=True, slots=True)
class ProposalRevisionRequestResult:
    proposal_id: UUID
    base_version: int
    workflow_run_id: UUID
    revision_job_id: UUID
    replayed: bool


@dataclass(frozen=True, slots=True)
class ProposalAIRevisionPreflight:
    run: WorkflowRun
    proposal: Proposal
    version: ProposalVersion
    approval: Approval
    locale: str


@dataclass(frozen=True, slots=True)
class WorkflowRunSnapshot:
    run: WorkflowRun
    checkpoint: WorkflowCheckpoint | None
    proposal: Proposal | None
    proposal_version: ProposalVersion | None
    events: tuple[WorkflowEvent, ...]
    previous_proposal_version: ProposalVersion | None = None


class PlanningRuntimePort(Protocol):
    """Backend-owned view of the AI runtime; no framework types cross it."""

    workflow_version: str
    verifier_version: str

    def validate_capability(self, message: str) -> None: ...

    def validate_proposal_content(self, content: dict[str, object]) -> dict[str, object]: ...

    def validate_proposal_deterministically(
        self,
        content: dict[str, object],
        *,
        active_membership_ids: frozenset[UUID],
    ) -> dict[str, object]: ...
