"""RLS transactions, immutable windows and append-only schedule configuration."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import Date, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.audit.domain.events import AuditOutcome
from app.modules.identity.adapters.database_models import UserModel
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.adapters.database_models import MembershipModel
from app.modules.planning_runs.adapters.database_models import OutboxEventModel
from app.modules.progress.adapters.daily_update_models import TaskProgressObservationModel
from app.modules.progress.adapters.daily_update_repository import SqlAlchemyDailyUpdateRepository
from app.modules.work.adapters.database_models import ProjectModel, TaskModel

from ..domain.schedules import (
    DailySummarySchedule,
    Reporter,
    ReportingWindow,
    ScheduleDraft,
    ScheduleError,
)
from ..domain.windows import resolve_window
from .database_models import (
    ReportingWindowModel,
    ScheduleDraftModel,
    ScheduleModel,
    ScheduleRecipientModel,
    ScheduleVersionModel,
    WindowReporterModel,
)


class ScheduleRepository(SqlAlchemyDailyUpdateRepository):
    async def authenticate(self) -> None:
        await super().authenticate()
        role = await self.session.scalar(
            select(MembershipModel.role).where(
                MembershipModel.organization_id == self.org,
                MembershipModel.id == self.actor.membership_id,
            )
        )
        if role not in {"MANAGER", "ADMIN"}:
            raise ScheduleError("FORBIDDEN", 403)

    async def authorize(self, project_id: UUID) -> None:
        project = await self.session.scalar(
            select(ProjectModel)
            .where(ProjectModel.organization_id == self.org, ProjectModel.id == project_id)
            .with_for_update()
        )
        if project is None:
            raise ScheduleError("RESOURCE_NOT_FOUND", 404)

    async def recipients(self, project_id: UUID) -> tuple[Reporter, ...]:
        rows = await self.session.execute(
            text(
                "SELECT DISTINCT m.id,u.display_name FROM memberships m "
                "JOIN users u ON u.id=m.user_id "
                "WHERE m.organization_id=:org AND m.is_active AND u.is_active AND "
                "(m.role IN ('MANAGER','ADMIN') OR EXISTS(SELECT 1 FROM tasks t "
                "WHERE t.organization_id=:org AND t.project_id=:project "
                "AND t.assignee_membership_id=m.id)) "
                "ORDER BY m.id"
            ),
            {"org": self.org, "project": project_id},
        )
        return tuple(Reporter(membership_id=row[0], name=row[1]) for row in rows)

    async def current(self, project_id: UUID) -> DailySummarySchedule | None:
        row = await self.session.scalar(
            select(ScheduleModel).where(
                ScheduleModel.organization_id == self.org, ScheduleModel.project_id == project_id
            )
        )
        return await self._version(row, row.version) if row else None

    async def by_id(self, schedule_id: UUID) -> DailySummarySchedule:
        row = await self.session.scalar(
            select(ScheduleModel).where(
                ScheduleModel.organization_id == self.org, ScheduleModel.id == schedule_id
            )
        )
        if row is None:
            raise ScheduleError("RESOURCE_NOT_FOUND", 404)
        return await self._version(row, row.version)

    async def _version(self, row: ScheduleModel, version: int) -> DailySummarySchedule:
        config = await self.session.scalar(
            select(ScheduleVersionModel).where(
                ScheduleVersionModel.organization_id == self.org,
                ScheduleVersionModel.schedule_id == row.id,
                ScheduleVersionModel.version == version,
            )
        )
        if config is None:
            raise ScheduleError("RESOURCE_NOT_FOUND", 404)
        recipients = tuple(
            await self.session.scalars(
                select(ScheduleRecipientModel.membership_id)
                .where(
                    ScheduleRecipientModel.organization_id == self.org,
                    ScheduleRecipientModel.schedule_id == row.id,
                    ScheduleRecipientModel.version == version,
                )
                .order_by(ScheduleRecipientModel.membership_id)
            )
        )
        return DailySummarySchedule.model_validate(
            {
                **config.payload,
                "id": row.id,
                "project_id": row.project_id,
                "version": version,
                "paused": row.paused,
                "recipients": recipients,
                "effective_at": config.effective_at,
            }
        )

    async def save(self, schedule: DailySummarySchedule) -> None:
        row = await self.session.scalar(
            select(ScheduleModel).where(
                ScheduleModel.organization_id == self.org, ScheduleModel.id == schedule.id
            )
        )
        if row is None:
            row = ScheduleModel(
                id=schedule.id,
                organization_id=self.org,
                project_id=schedule.project_id,
                version=schedule.version,
                paused=schedule.paused,
            )
            self.session.add(row)
        else:
            if row.version != schedule.version - 1:
                raise ScheduleError("STALE_SCHEDULE")
            row.version = schedule.version
            row.paused = schedule.paused
        await self.session.flush()
        self.session.add(
            ScheduleVersionModel(
                organization_id=self.org,
                schedule_id=row.id,
                version=schedule.version,
                effective_at=schedule.effective_at,
                payload=schedule.model_dump(
                    mode="json",
                    exclude={"id", "project_id", "version", "paused", "recipients", "effective_at"},
                ),
            )
        )
        await self.session.flush()
        self.session.add_all(
            [
                ScheduleRecipientModel(
                    organization_id=self.org,
                    schedule_id=row.id,
                    version=schedule.version,
                    membership_id=member,
                )
                for member in schedule.recipients
            ]
        )
        await self.session.flush()
        self.session.add(
            OutboxEventModel(
                id=uuid4(),
                organization_id=self.org,
                event_id=uuid4(),
                event_type="automation.schedule.changed.v1",
                aggregate_type="daily_summary_schedule",
                aggregate_id=row.id,
                payload={"schedule_id": str(row.id), "version": row.version, "paused": row.paused},
            )
        )

    async def store_preview(self, draft: ScheduleDraft) -> None:
        self.session.add(
            ScheduleDraftModel(
                id=draft.id,
                organization_id=self.org,
                project_id=draft.command.project_id,
                owner_membership_id=self.actor.membership_id,
                expires_at=draft.expires_at,
                payload=draft.model_dump(mode="json"),
            )
        )
        await self.session.flush()

    async def preview_by_id(self, draft_id: UUID) -> ScheduleDraft:
        row = await self.session.scalar(
            select(ScheduleDraftModel).where(
                ScheduleDraftModel.organization_id == self.org,
                ScheduleDraftModel.id == draft_id,
                ScheduleDraftModel.owner_membership_id == self.actor.membership_id,
            )
        )
        if row is None:
            raise ScheduleError("RESOURCE_NOT_FOUND", 404)
        return ScheduleDraft.model_validate(row.payload)

    async def roster(self, project_id: UUID) -> tuple[UUID, ...]:
        values = await self.session.scalars(
            select(TaskModel.assignee_membership_id)
            .join(
                MembershipModel,
                (MembershipModel.organization_id == TaskModel.organization_id)
                & (MembershipModel.id == TaskModel.assignee_membership_id),
            )
            .join(UserModel, UserModel.id == MembershipModel.user_id)
            .where(
                TaskModel.organization_id == self.org,
                TaskModel.project_id == project_id,
                TaskModel.status != "DONE",
                MembershipModel.is_active,
                UserModel.is_active,
            )
            .distinct()
            .order_by(TaskModel.assignee_membership_id)
        )
        return tuple(member for member in values if member is not None)

    async def roster_at(self, project_id: UUID, at: datetime) -> tuple[UUID, ...]:
        rows = await self.session.scalars(
            text(
                "SELECT DISTINCT assignee_membership_id FROM ("
                "SELECT DISTINCT ON (h.task_id) h.assignee_membership_id,"
                "h.status,h.reporter_active "
                "FROM automation_task_scope_history h JOIN tasks t "
                "ON t.organization_id=h.organization_id AND t.id=h.task_id "
                "WHERE h.organization_id=:org AND t.project_id=:project AND h.recorded_at<=:at "
                "ORDER BY h.task_id,h.recorded_at DESC,h.id DESC) historical "
                "WHERE assignee_membership_id IS NOT NULL AND status<>'DONE' AND reporter_active "
                "ORDER BY assignee_membership_id"
            ),
            {"org": self.org, "project": project_id, "at": at},
        )
        return tuple(UUID(str(value)) for value in rows)

    async def window(self, schedule: DailySummarySchedule, at: datetime) -> ReportingWindow:
        row = await self.session.scalar(
            select(ReportingWindowModel).where(
                ReportingWindowModel.organization_id == self.org,
                ReportingWindowModel.schedule_id == schedule.id,
                ReportingWindowModel.starts_at <= at,
                ReportingWindowModel.ends_at > at,
            )
        )
        live = await self.roster(schedule.project_id)
        if row is None:
            version = await self.session.scalar(
                select(ScheduleVersionModel.version)
                .where(
                    ScheduleVersionModel.organization_id == self.org,
                    ScheduleVersionModel.schedule_id == schedule.id,
                    ScheduleVersionModel.effective_at <= at,
                )
                .order_by(
                    ScheduleVersionModel.effective_at.desc(), ScheduleVersionModel.version.desc()
                )
                .limit(1)
            )
            if version is None:
                raise ScheduleError("WINDOW_NOT_ACTIVE")
            schedule_row = await self.session.scalar(
                select(ScheduleModel).where(
                    ScheduleModel.organization_id == self.org, ScheduleModel.id == schedule.id
                )
            )
            assert schedule_row is not None
            applied = await self._version(schedule_row, version)
            boundaries = resolve_window(applied.model_copy(update={"paused": False}), at)
            frozen = await self.roster_at(schedule.project_id, boundaries.starts_at)
            window = boundaries.model_copy(
                update={
                    "expected_reporters": frozen,
                    "coverage_state": "PARTIAL" if frozen else "NO_REPORTERS",
                    "roster_observed_at": at,
                }
            )
            row = ReportingWindowModel(
                id=window.id,
                organization_id=self.org,
                schedule_id=schedule.id,
                applied_version=version,
                starts_at=window.starts_at,
                ends_at=window.ends_at,
                payload=window.model_dump(
                    mode="json", exclude={"expected_reporters", "reported_members"}
                ),
            )
            self.session.add(row)
            await self.session.flush()
            self.session.add_all(
                [
                    WindowReporterModel(
                        organization_id=self.org, window_id=row.id, membership_id=member
                    )
                    for member in frozen
                ]
            )
            await self.session.flush()
        expected = tuple(
            await self.session.scalars(
                select(WindowReporterModel.membership_id)
                .where(
                    WindowReporterModel.organization_id == self.org,
                    WindowReporterModel.window_id == row.id,
                )
                .order_by(WindowReporterModel.membership_id)
            )
        )
        window = ReportingWindow.model_validate({**row.payload, "expected_reporters": expected})
        # Coverage uses confirmed facts and reporter/project scope, never Task completion.
        reported = tuple(
            await self.session.scalars(
                select(TaskProgressObservationModel.owner_membership_id)
                .join(
                    TaskModel,
                    (TaskModel.organization_id == TaskProgressObservationModel.organization_id)
                    & (TaskModel.id == TaskProgressObservationModel.task_id),
                )
                .where(
                    TaskProgressObservationModel.organization_id == self.org,
                    TaskModel.project_id == schedule.project_id,
                    TaskProgressObservationModel.owner_membership_id.in_(expected),
                    TaskProgressObservationModel.confirmed_at >= window.starts_at,
                    func.timezone(window.timezone, TaskProgressObservationModel.reporting_at).cast(
                        Date
                    )
                    == date.fromisoformat(window.local_date),
                    TaskProgressObservationModel.confirmed_at < window.ends_at,
                    TaskProgressObservationModel.confirmed_at <= at,
                )
                .distinct()
                .order_by(TaskProgressObservationModel.owner_membership_id)
            )
        )
        complete = bool(expected) and set(expected) <= set(reported)
        return window.model_copy(
            update={
                "reported_members": reported,
                "full_coverage": complete,
                "coverage_state": "COMPLETE"
                if complete
                else "PARTIAL"
                if expected
                else "NO_REPORTERS",
                "scope_changed": set(expected) != set(live),
                "enabled": window.enabled and not schedule.paused,
            }
        )

    async def record_audit(
        self, action: str, key: str, resource_id: UUID | None, code: str | None = None
    ) -> None:
        self.session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id,
                action=action,
                outcome=AuditOutcome.REJECTED if code else AuditOutcome.SUCCEEDED,
                resource_type="daily_summary_schedule",
                resource_id=resource_id,
                request_id=key or "schedule",
                idempotency_key=key or None,
                before_data={},
                after_data={},
                reason_data={"code": code} if code else {},
            )
        )


class ScheduleTransactions:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self.sessions = sessions

    @asynccontextmanager
    async def __call__(self, actor: AuthenticatedActor) -> AsyncGenerator[ScheduleRepository]:
        async with self.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true),"
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            yield ScheduleRepository(session, actor)
