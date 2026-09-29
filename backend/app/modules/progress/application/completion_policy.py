"""Load current, tenant-scoped completion facts in the Task status transaction."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.progress.adapters.daily_update_models import (
    DailyUpdateEvidenceLinkModel,
    TaskActualProjectionModel,
    TaskProgressObservationModel,
)
from app.modules.progress.adapters.evidence_models import EvidenceOriginalModel
from app.modules.progress.domain.completion import (
    CompletionCriterion,
    CompletionObservation,
    CompletionRequirementError,
    CriterionAttestation,
    validate_completion,
)
from app.modules.progress.domain.daily_updates import SelectedEvidence
from app.modules.work.adapters.database_models import TaskStatusTransitionModel
from app.modules.work.domain.tasks import TaskStatus
from app.modules.work.planning.adapters.database_models import AcceptanceCriterionModel


async def validate_status_completion(
    session: AsyncSession,
    actor: AuthenticatedActor,
    task_id: UUID,
    target: TaskStatus,
    attestations: tuple[CriterionAttestation, ...],
) -> tuple[CompletionObservation | None, tuple[CompletionCriterion, ...]]:
    if target is not TaskStatus.DONE:
        validate_completion(actor.role.value, target, None, (), attestations, None)
        return None, ()
    criteria_rows = (
        await session.scalars(
            select(AcceptanceCriterionModel)
            .where(
                AcceptanceCriterionModel.organization_id == actor.organization_id,
                AcceptanceCriterionModel.task_id == task_id,
            )
            .order_by(AcceptanceCriterionModel.id)
            .with_for_update(read=True)
        )
    ).all()
    criteria = tuple(CompletionCriterion(row.id, row.version) for row in criteria_rows)
    projection = await session.scalar(
        select(TaskActualProjectionModel).where(
            TaskActualProjectionModel.organization_id == actor.organization_id,
            TaskActualProjectionModel.task_id == task_id,
        )
    )
    observation: CompletionObservation | None = None
    if projection and projection.observation_id:
        row = await session.scalar(
            select(TaskProgressObservationModel).where(
                TaskProgressObservationModel.organization_id == actor.organization_id,
                TaskProgressObservationModel.id == projection.observation_id,
                TaskProgressObservationModel.task_id == task_id,
            )
        )
        if row and (
            actor.role.value != "EMPLOYEE" or row.owner_membership_id == actor.membership_id
        ):
            links = (
                await session.scalars(
                    select(DailyUpdateEvidenceLinkModel)
                    .join(
                        EvidenceOriginalModel,
                        (
                            EvidenceOriginalModel.organization_id
                            == DailyUpdateEvidenceLinkModel.organization_id
                        )
                        & (EvidenceOriginalModel.id == DailyUpdateEvidenceLinkModel.evidence_id)
                        & (
                            EvidenceOriginalModel.version
                            == DailyUpdateEvidenceLinkModel.evidence_version
                        ),
                    )
                    .where(
                        DailyUpdateEvidenceLinkModel.organization_id == actor.organization_id,
                        DailyUpdateEvidenceLinkModel.observation_id == row.id,
                        EvidenceOriginalModel.state == "READY",
                    )
                )
            ).all()
            observation = CompletionObservation(
                row.id,
                row.reported_percent,
                tuple(
                    SelectedEvidence(evidence_id=link.evidence_id, version=link.evidence_version)
                    for link in links
                ),
                row.confirmed_at,
            )
    reopened_at: datetime | None = await session.scalar(
        select(TaskStatusTransitionModel.occurred_at)
        .where(
            TaskStatusTransitionModel.organization_id == actor.organization_id,
            TaskStatusTransitionModel.task_id == task_id,
            TaskStatusTransitionModel.from_status == TaskStatus.DONE,
            TaskStatusTransitionModel.to_status == TaskStatus.IN_PROGRESS,
        )
        .order_by(TaskStatusTransitionModel.occurred_at.desc())
        .limit(1)
    )
    validate_completion(actor.role.value, target, observation, criteria, attestations, reopened_at)
    # Optional criterion sources must be eligible and linked to this Task.
    for attestation in attestations:
        if len(set(attestation.evidence_refs)) != len(attestation.evidence_refs):
            raise CompletionRequirementError("COMPLETION_EVIDENCE_INVALID")
        for ref in attestation.evidence_refs:
            exists = await session.scalar(
                select(DailyUpdateEvidenceLinkModel.id)
                .join(
                    TaskProgressObservationModel,
                    (
                        TaskProgressObservationModel.organization_id
                        == DailyUpdateEvidenceLinkModel.organization_id
                    )
                    & (
                        TaskProgressObservationModel.id
                        == DailyUpdateEvidenceLinkModel.observation_id
                    ),
                )
                .join(
                    EvidenceOriginalModel,
                    (
                        EvidenceOriginalModel.organization_id
                        == DailyUpdateEvidenceLinkModel.organization_id
                    )
                    & (EvidenceOriginalModel.id == DailyUpdateEvidenceLinkModel.evidence_id)
                    & (
                        EvidenceOriginalModel.version
                        == DailyUpdateEvidenceLinkModel.evidence_version
                    ),
                )
                .where(
                    DailyUpdateEvidenceLinkModel.organization_id == actor.organization_id,
                    TaskProgressObservationModel.task_id == task_id,
                    DailyUpdateEvidenceLinkModel.evidence_id == ref.evidence_id,
                    DailyUpdateEvidenceLinkModel.evidence_version == ref.version,
                    EvidenceOriginalModel.state == "READY",
                )
                .limit(1)
            )
            if exists is None:
                raise CompletionRequirementError("COMPLETION_EVIDENCE_INVALID")
    return observation, criteria
