"""Deterministic requirements for an explicit Task completion declaration."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from app.modules.progress.domain.daily_updates import SelectedEvidence
from app.modules.work.domain.tasks import TaskError, TaskStatus

EvidenceVersionRef = SelectedEvidence


class CompletionRequirementError(TaskError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CompletionCriterion:
    id: UUID
    version: int


@dataclass(frozen=True)
class CriterionAttestation:
    criterion_id: UUID
    version: int
    confirmed: bool
    evidence_refs: tuple[EvidenceVersionRef, ...] = ()


@dataclass(frozen=True)
class CompletionObservation:
    id: UUID
    reported_percent: Decimal
    evidence_refs: tuple[EvidenceVersionRef, ...]
    confirmed_at: datetime


@dataclass(frozen=True)
class CompletionDecision:
    allowed: bool = True


def validate_completion(
    actor_role: str,
    target: TaskStatus,
    latest_observation: CompletionObservation | None,
    current_criteria: tuple[CompletionCriterion, ...],
    attestations: tuple[CriterionAttestation, ...],
    reopened_at: datetime | None,
) -> CompletionDecision:
    if target is not TaskStatus.DONE:
        if attestations:
            raise CompletionRequirementError("COMPLETION_ATTESTATIONS_UNEXPECTED")
        return CompletionDecision()
    if actor_role == "EMPLOYEE" and (
        latest_observation is None
        or latest_observation.reported_percent != 100
        or not latest_observation.evidence_refs
        or (reopened_at is not None and latest_observation.confirmed_at <= reopened_at)
    ):
        raise CompletionRequirementError("COMPLETION_REPORT_REQUIRED")
    expected = {criterion.id: criterion.version for criterion in current_criteria}
    supplied = {item.criterion_id: item for item in attestations}
    if len(supplied) != len(attestations):
        raise CompletionRequirementError("COMPLETION_CRITERIA_STALE")
    if set(supplied) != set(expected):
        raise CompletionRequirementError("COMPLETION_CRITERIA_REQUIRED")
    if any(
        not item.confirmed or item.version != version
        for criterion_id, version in expected.items()
        if (item := supplied[criterion_id])
    ):
        raise CompletionRequirementError("COMPLETION_CRITERIA_STALE")
    return CompletionDecision()
