"""Read current-authorized business evidence, never model text or feedback sentiment."""

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.progress.adapters.blocker_models import BlockerModel, BlockerTransitionModel
from app.modules.progress.adapters.daily_update_models import (
    TaskActualProjectionModel,
    TaskProgressObservationModel,
)
from app.modules.progress.domain.blockers import BlockerTransition
from app.modules.reporting.domain.reports import ReportError
from app.modules.work.adapters.database_models import TaskModel, TaskStatusTransitionModel

from ..domain.outcomes import OutcomeFacts, OutcomeSourceCommand


class SQLOutcomeReader:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def read_outcome(
        self, actor: AuthenticatedActor, source: OutcomeSourceCommand
    ) -> OutcomeFacts:
        org = actor.organization_id
        facts: dict[str, JsonValue] = {}
        occurred_at = None
        observation_id = None
        state = "AVAILABLE"
        if source.source_type == "TASK_TRANSITION":
            row = await self.session.scalar(
                select(TaskStatusTransitionModel).where(
                    TaskStatusTransitionModel.organization_id == org,
                    TaskStatusTransitionModel.id == source.source_id,
                )
            )
            if row is None:
                raise ReportError("RESOURCE_NOT_FOUND", 404)
            if row.task_version_after != source.source_version:
                raise ReportError("OUTCOME_SOURCE_STALE", 412)
            task_id = row.task_id
            facts = {"from_status": row.from_status.value, "to_status": row.to_status.value}
            occurred_at = row.occurred_at
        elif source.source_type == "BLOCKER_RESOLUTION":
            transition = await self.session.scalar(
                select(BlockerTransitionModel).where(
                    BlockerTransitionModel.organization_id == org,
                    BlockerTransitionModel.id == source.source_id,
                )
            )
            if transition is None:
                raise ReportError("RESOURCE_NOT_FOUND", 404)
            if transition.version != source.source_version:
                raise ReportError("OUTCOME_SOURCE_STALE", 412)
            blocker = await self.session.scalar(
                select(BlockerModel).where(
                    BlockerModel.organization_id == org, BlockerModel.id == transition.blocker_id
                )
            )
            if blocker is None:
                raise ReportError("RESOURCE_NOT_FOUND", 404)
            task_id = blocker.task_id
            # Read only typed lifecycle metadata; descriptions/payload instructions are not facts.
            status = BlockerTransition.model_validate(transition.payload).to_status
            facts = {"blocker_id": str(blocker.id), "status": status}
            occurred_at = transition.at
            state = "AVAILABLE" if status == "RESOLVED" else "UNKNOWN"
        else:
            task_id = source.source_id
            projection = await self.session.scalar(
                select(TaskActualProjectionModel).where(
                    TaskActualProjectionModel.organization_id == org,
                    TaskActualProjectionModel.task_id == task_id,
                )
            )
            if (projection.progress_version if projection else 0) != source.source_version:
                raise ReportError("OUTCOME_SOURCE_STALE", 412)
            observation = await self.session.scalar(
                select(TaskProgressObservationModel).where(
                    TaskProgressObservationModel.organization_id == org,
                    TaskProgressObservationModel.id
                    == (projection.observation_id if projection else None),
                )
            )
            state = "UNKNOWN"
            if observation:
                if (
                    observation.task_id != task_id
                    or observation.progress_version != source.source_version
                ):
                    raise ReportError("OUTCOME_SOURCE_INVALID", 422)
                observation_id = observation.id
                # Confirmed self-report is observed progress, never completion acceptance.
                facts = {
                    "reported_percent": str(observation.reported_percent),
                    "remaining_hours": str(observation.remaining_hours)
                    if observation.remaining_hours is not None
                    else None,
                    "observation_id": str(observation.id),
                    "completion_state": "UNKNOWN",
                }
                occurred_at = observation.confirmed_at
                state = "AVAILABLE"
        task = await self.session.scalar(
            select(TaskModel).where(TaskModel.organization_id == org, TaskModel.id == task_id)
        )
        if task is None:
            raise ReportError("RESOURCE_NOT_FOUND", 404)
        facts["task_id"] = str(task.id)
        return OutcomeFacts(
            project_id=task.project_id,
            source=source,
            state=state,
            facts=facts,
            occurred_at=occurred_at,
            observation_id=observation_id,
        )
