"""Transaction-local capture and read-only weekly projections."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.audit.domain.events import AuditOutcome
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.planning_runs.adapters.database_models import OutboxEventModel
from app.modules.progress.adapters.daily_update_models import (
    TaskActualProjectionModel,
    TaskProgressObservationModel,
    WeeklyActualSnapshotModel,
)
from app.modules.progress.adapters.progress_models import WeeklyPlanBaselineModel
from app.modules.progress.application.progress_ports import ProgressRepository
from app.modules.progress.domain.weekly_progress import PlanBaseline, PlanEntry, TaskActual
from app.modules.work.adapters.database_models import ProjectModel, TaskModel
from app.modules.work.planning.adapters.database_models import ProjectWeekModel, TaskDependencyModel


def plan_from_model(model: WeeklyPlanBaselineModel) -> PlanBaseline:
    payload = model.payload
    return PlanBaseline(
        model.id,
        model.project_week_id,
        tuple(
            PlanEntry(
                UUID(entry["task_id"]),
                entry["task_version"],
                entry["title"],
                Decimal(entry["effort_hours"]) if entry["effort_hours"] is not None else None,
                date.fromisoformat(entry["due_date"]) if entry["due_date"] else None,
                tuple(UUID(value) for value in entry["predecessor_ids"]),
                entry.get("captured_status", "UNKNOWN"),
            )
            for entry in payload["task_entries"]
        ),
        model.captured_at,
        model.kind,
        date.fromisoformat(payload["start_date"]),
        date.fromisoformat(payload["end_date"]),
        payload["week_version"],
        model.sealed_at,
    )


async def capture_project_baselines(
    session: AsyncSession,
    actor: AuthenticatedActor,
    project_id: UUID,
    kind: str = "MANUAL",
    request_id: str = "weekly-plan-capture",
    idempotency_key: str | None = None,
) -> None:
    """Called after graph validation, within its existing transaction/project lock."""
    await session.flush()
    await session.scalar(
        select(ProjectModel.id)
        .where(ProjectModel.organization_id == actor.organization_id, ProjectModel.id == project_id)
        .with_for_update()
    )
    weeks = (
        await session.scalars(
            select(ProjectWeekModel)
            .where(
                ProjectWeekModel.organization_id == actor.organization_id,
                ProjectWeekModel.project_id == project_id,
            )
            .order_by(ProjectWeekModel.id)
        )
    ).all()
    tasks = (
        await session.scalars(
            select(TaskModel)
            .where(
                TaskModel.organization_id == actor.organization_id,
                TaskModel.project_id == project_id,
            )
            .order_by(TaskModel.id)
        )
    ).all()
    edges = (
        await session.scalars(
            select(TaskDependencyModel)
            .where(
                TaskDependencyModel.organization_id == actor.organization_id,
                TaskDependencyModel.successor_task_id.in_([task.id for task in tasks]),
            )
            .order_by(TaskDependencyModel.predecessor_task_id)
        )
    ).all()
    for week in weeks:
        latest = await session.scalar(
            select(WeeklyPlanBaselineModel)
            .where(
                WeeklyPlanBaselineModel.organization_id == actor.organization_id,
                WeeklyPlanBaselineModel.project_week_id == week.id,
            )
            .order_by(WeeklyPlanBaselineModel.sequence.desc())
            .limit(1)
        )
        if latest is not None and latest.sealed_at is not None:
            continue
        payload: dict[str, Any] = {
            "start_date": week.start_date.isoformat(),
            "end_date": week.end_date.isoformat(),
            "week_version": week.version,
            "status": week.status,
            "objective": week.objective,
            "task_entries": [
                {
                    "task_id": str(task.id),
                    "task_version": task.version,
                    "title": task.title,
                    "captured_status": str(task.status),
                    "effort_hours": str(task.estimated_effort_hours)
                    if task.estimated_effort_hours is not None
                    else None,
                    "due_date": task.due_date.isoformat() if task.due_date else None,
                    "predecessor_ids": [
                        str(edge.predecessor_task_id)
                        for edge in edges
                        if edge.successor_task_id == task.id
                    ],
                }
                for task in tasks
                if task.project_week_id == week.id
            ],
        }

        if week.status == "COMPLETED":
            original = await session.scalar(
                select(WeeklyPlanBaselineModel)
                .where(
                    WeeklyPlanBaselineModel.organization_id == actor.organization_id,
                    WeeklyPlanBaselineModel.project_week_id == week.id,
                )
                .order_by(WeeklyPlanBaselineModel.sequence)
                .limit(1)
            )
            original_ids: set[UUID] = (
                {UUID(entry["task_id"]) for entry in original.payload["task_entries"]}
                if original
                else set()
            )
            current_ids = {UUID(entry["task_id"]) for entry in payload["task_entries"]}
            frozen_actuals = await load_task_actuals(
                session, actor, [task for task in tasks if task.id in original_ids | current_ids]
            )
            payload["sealed_actuals"] = [actual_payload(actual) for actual in frozen_actuals]

        # Task version provenance is updated only when the plan facts change.
        def comparable(value: dict[str, Any]) -> dict[str, Any]:
            return {
                **value,
                "task_entries": [
                    {k: v for k, v in entry.items() if k not in ("task_version", "captured_status")}
                    for entry in value["task_entries"]
                ],
            }

        if latest is not None and comparable(latest.payload) == comparable(payload):
            continue
        now = datetime.now(UTC)
        snapshot_id = uuid4()
        session.add(
            WeeklyPlanBaselineModel(
                id=snapshot_id,
                organization_id=actor.organization_id,
                project_week_id=week.id,
                sequence=latest.sequence + 1 if latest else 1,
                kind=kind,
                captured_at=now,
                sealed_at=now if week.status == "COMPLETED" else None,
                payload=payload,
            )
        )
        if week.status == "COMPLETED":
            session.add(
                WeeklyActualSnapshotModel(
                    id=uuid4(),
                    organization_id=actor.organization_id,
                    project_week_id=week.id,
                    owner_membership_id=actor.membership_id,
                    kind="FINAL",
                    captured_at=now,
                    payload={"actuals": payload["sealed_actuals"]},
                )
            )
        session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=actor.organization_id,
                actor_membership_id=actor.membership_id,
                action="weekly_plan.captured",
                outcome=AuditOutcome.SUCCEEDED,
                resource_type="weekly_plan_baseline",
                resource_id=snapshot_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                before_data={},
                after_data={
                    "project_week_id": str(week.id),
                    "sequence": latest.sequence + 1 if latest else 1,
                },
                reason_data={"kind": kind},
            )
        )
        event_id = uuid4()
        session.add(
            OutboxEventModel(
                id=event_id,
                organization_id=actor.organization_id,
                event_id=event_id,
                event_type="weekly_plan.captured",
                aggregate_type="weekly_plan_baseline",
                aggregate_id=snapshot_id,
                envelope_version="1.0",
                payload={"project_week_id": str(week.id), "baseline_id": str(snapshot_id)},
            )
        )
    await session.flush()


def actual_payload(actual: TaskActual) -> dict[str, Any]:
    return {
        "task_id": str(actual.task_id),
        "progress_version": actual.progress_version,
        "reported_percent": str(actual.reported_percent)
        if actual.reported_percent is not None
        else None,
        "remaining_hours": str(actual.remaining_hours)
        if actual.remaining_hours is not None
        else None,
        "observation_id": str(actual.observation_id) if actual.observation_id else None,
        "reporting_at": actual.reporting_at.isoformat() if actual.reporting_at else None,
        "status": actual.status,
        "stale": actual.stale,
    }


def actual_from_payload(item: dict[str, Any]) -> TaskActual:
    return TaskActual(
        UUID(item["task_id"]),
        item.get("progress_version", 0),
        Decimal(item["reported_percent"]) if item.get("reported_percent") is not None else None,
        Decimal(item["remaining_hours"]) if item.get("remaining_hours") is not None else None,
        UUID(item["observation_id"]) if item.get("observation_id") else None,
        datetime.fromisoformat(item["reporting_at"]) if item.get("reporting_at") else None,
        item["status"],
        item.get("stale", False),
    )


async def load_task_actuals(
    session: AsyncSession, actor: AuthenticatedActor, tasks: list[TaskModel]
) -> tuple[TaskActual, ...]:
    rows = (
        await session.execute(
            select(TaskActualProjectionModel, TaskProgressObservationModel)
            .outerjoin(
                TaskProgressObservationModel,
                (
                    TaskActualProjectionModel.organization_id
                    == TaskProgressObservationModel.organization_id
                )
                & (TaskActualProjectionModel.observation_id == TaskProgressObservationModel.id),
            )
            .where(
                TaskActualProjectionModel.organization_id == actor.organization_id,
                TaskActualProjectionModel.task_id.in_([task.id for task in tasks]),
            )
        )
    ).all()
    lookup = {projection.task_id: (projection, observation) for projection, observation in rows}
    result: list[TaskActual] = []
    for task in tasks:
        pair = lookup.get(task.id)
        projection, observation = pair if pair else (None, None)
        stale = bool(
            observation and (observation.owner_membership_id != task.assignee_membership_id)
        )
        result.append(
            TaskActual(
                task.id,
                projection.progress_version if projection else 0,
                observation.reported_percent if observation else None,
                observation.remaining_hours if observation else None,
                observation.id if observation else None,
                observation.reporting_at if observation else None,
                str(task.status),
                stale,
            )
        )
    return tuple(result)


class SqlAlchemyProgressRepository:
    def __init__(self, session: AsyncSession, actor: AuthenticatedActor):
        self.session = session
        self.actor = actor

    async def get(
        self, project_id: UUID, week_id: UUID
    ) -> tuple[PlanBaseline, PlanBaseline, tuple[TaskActual, ...]] | None:
        # Authenticate the current database role as well as the request actor.
        permitted = await self.session.scalar(
            text(
                "SELECT public.lock_active_membership(:org, :member) AND EXISTS (SELECT 1 "
                "FROM memberships WHERE organization_id=:org AND id=:member AND "
                "user_id=:user AND role IN ('MANAGER','ADMIN'))"
            ),
            {
                "org": self.actor.organization_id,
                "member": self.actor.membership_id,
                "user": self.actor.user_id,
            },
        )
        if permitted is not True:
            from app.modules.progress.application.weekly_progress_service import (
                ProgressForbiddenError,
            )

            raise ProgressForbiddenError
        # A shared project lock prevents mixing snapshot/status from separate plan revisions.
        project = await self.session.scalar(
            select(ProjectModel.id)
            .where(
                ProjectModel.organization_id == self.actor.organization_id,
                ProjectModel.id == project_id,
            )
            .with_for_update(read=True)
        )
        if project is None:
            return None
        week = await self.session.scalar(
            select(ProjectWeekModel).where(
                ProjectWeekModel.organization_id == self.actor.organization_id,
                ProjectWeekModel.project_id == project_id,
                ProjectWeekModel.id == week_id,
            )
        )
        if week is None:
            return None
        models = (
            await self.session.scalars(
                select(WeeklyPlanBaselineModel)
                .where(
                    WeeklyPlanBaselineModel.organization_id == self.actor.organization_id,
                    WeeklyPlanBaselineModel.project_week_id == week_id,
                )
                .order_by(WeeklyPlanBaselineModel.sequence)
            )
        ).all()
        if not models:
            return None
        original, current = plan_from_model(models[0]), plan_from_model(models[-1])
        if current.sealed_at is not None:
            # Freeze the union of original/current tasks at completion, including moved work.
            frozen = models[-1].payload.get("sealed_actuals")
            if frozen is None:
                # Entry baselines for weeks already completed at activation.
                frozen = [
                    {"task_id": str(entry.task_id), "status": entry.captured_status}
                    for entry in current.task_entries
                ]
            return (
                original,
                current,
                tuple(actual_from_payload(item) for item in frozen),
            )
        ids = {entry.task_id for plan in (original, current) for entry in plan.task_entries}
        tasks = (
            await self.session.scalars(
                select(TaskModel).where(
                    TaskModel.organization_id == self.actor.organization_id, TaskModel.id.in_(ids)
                )
            )
        ).all()
        actuals = await load_task_actuals(self.session, self.actor, list(tasks))
        return original, current, actuals


class SqlAlchemyProgressTransactionFactory:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self.sessions = sessions

    @asynccontextmanager
    async def __call__(self, actor: AuthenticatedActor) -> AsyncGenerator[ProgressRepository]:
        async with self.sessions.begin() as session:
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text(
                    "SELECT set_config('app.organization_id', :org, true), "
                    "set_config('app.membership_id', :member, true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            yield SqlAlchemyProgressRepository(session, actor)
