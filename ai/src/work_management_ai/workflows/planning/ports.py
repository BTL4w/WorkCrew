"""Persistence contracts owned by the planning workflow."""

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from work_management_ai.schemas.planning import PlanningModelOutput
from work_management_ai.schemas.progressive_planning import DraftBinding, PlanningDecision
from work_management_ai.workflows.planning.draft_verifier import DraftValidationResult
from work_management_ai.workflows.planning.state import ProgressivePlanningState
from work_management_ai.workflows.planning.verifier import PlanningValidationResult


@dataclass(frozen=True, slots=True)
class PlanningCheckpoint:
    idempotency_key: str
    thread_id: str
    run_id: UUID
    organization_id: UUID
    node: str
    state: dict[str, object]


@dataclass(frozen=True, slots=True)
class PlanningProgressEvent:
    idempotency_key: str
    run_id: UUID
    organization_id: UUID
    stage: str
    public_payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class PlanningProposalDraft:
    idempotency_key: str
    run_id: UUID
    organization_id: UUID
    actor_membership_id: UUID
    content: PlanningModelOutput
    validation: PlanningValidationResult
    context_reference_ids: tuple[str, ...]
    workflow_version: str
    prompt_version: str
    schema_version: str
    model_reference: str
    verifier_version: str


@dataclass(frozen=True, slots=True)
class PersistedProposalReference:
    proposal_id: UUID
    version: int


@dataclass(frozen=True, slots=True)
class PlanningRevisionBase:
    proposal_id: UUID
    version: int
    locale: Literal["vi", "en"]
    content: PlanningModelOutput


@dataclass(frozen=True, slots=True)
class PlanningRevisionDraft:
    base_proposal_id: UUID
    base_version: int
    content: PlanningModelOutput
    change_summary: str
    model_reference: str


class PlanningPersistencePort(Protocol):
    """Only boundary through which graph state/progress/proposals are persisted."""

    async def save_checkpoint(self, checkpoint: PlanningCheckpoint) -> None: ...

    async def append_progress(self, event: PlanningProgressEvent) -> None: ...

    async def persist_proposal(
        self,
        draft: PlanningProposalDraft,
    ) -> PersistedProposalReference: ...


class ProgressivePlanningPersistencePort(Protocol):
    async def publish_progressive_result(
        self,
        state: ProgressivePlanningState,
        decision: PlanningDecision,
        validation: DraftValidationResult,
    ) -> DraftBinding | None: ...
