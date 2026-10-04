"""Read exact Phase 4 frozen roster and confirmed reporter facts, without opening windows."""

from datetime import date, datetime
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import Date, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.automations.adapters.database_models import WindowReporterModel
from app.modules.automations.adapters.repository import ScheduleRepository
from app.modules.automations.domain.schedules import ReportingWindow
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.progress.adapters.daily_update_models import TaskProgressObservationModel
from app.modules.work.adapters.database_models import TaskModel


async def window_facts(
    session: AsyncSession,
    actor: AuthenticatedActor,
    project_id: UUID,
    window: ReportingWindow,
    at: datetime,
) -> tuple[dict[str, JsonValue], tuple[TaskProgressObservationModel, ...]]:
    org = actor.organization_id
    expected = set(
        await session.scalars(
            select(WindowReporterModel.membership_id).where(
                WindowReporterModel.organization_id == org,
                WindowReporterModel.window_id == window.id,
            )
        )
    )
    observations = tuple(
        await session.scalars(
            select(TaskProgressObservationModel)
            .join(
                TaskModel,
                (TaskModel.organization_id == TaskProgressObservationModel.organization_id)
                & (TaskModel.id == TaskProgressObservationModel.task_id),
            )
            .where(
                TaskProgressObservationModel.organization_id == org,
                TaskModel.project_id == project_id,
                TaskProgressObservationModel.owner_membership_id.in_(expected),
                TaskProgressObservationModel.confirmed_at >= window.starts_at,
                TaskProgressObservationModel.confirmed_at < window.ends_at,
                TaskProgressObservationModel.confirmed_at <= at,
                func.timezone(window.timezone, TaskProgressObservationModel.reporting_at).cast(Date)
                == date.fromisoformat(window.local_date),
            )
        )
    )
    reported = {o.owner_membership_id for o in observations}
    live = set(await ScheduleRepository(session, actor).roster(project_id))
    facts: dict[str, JsonValue] = {
        "project_id": str(project_id),
        "local_date": window.local_date,
        "timezone": window.timezone,
        "starts_at": window.starts_at.isoformat(),
        "ends_at": window.ends_at.isoformat(),
        "expected_count": len(expected),
        "reported_count": len(reported),
        "expected_reporters": [str(id) for id in sorted(expected)],
        "reported_members": [str(id) for id in sorted(reported)],
        "observation_ids": [str(o.id) for o in sorted(observations, key=lambda o: o.id)],
        "coverage_state": "NO_REPORTERS"
        if not expected
        else "COMPLETE"
        if expected <= reported
        else "PARTIAL",
        "scope_changed": expected != live,
    }
    return facts, observations
