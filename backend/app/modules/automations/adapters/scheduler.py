"""Bounded PostgreSQL reconciliation; resume considers only the current reporting window."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.identity.adapters.current_actor import CurrentActorResolver

from ..application.digest_service import DigestService
from ..application.schedule_service import ScheduleService
from ..domain.digests import AuthorizedJobScope
from ..domain.schedules import ScheduleError
from .database_models import ScheduleModel
from .digest_repository import DigestTransactions
from .job_runner import JobRunner
from .repository import ScheduleRepository, ScheduleTransactions


class Scheduler:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], actors: CurrentActorResolver):
        self.sessions = sessions
        self.actors = actors
        self.schedules = ScheduleService(ScheduleTransactions(sessions))
        self.digests = DigestService(DigestTransactions(sessions))
        self.jobs = JobRunner(self.digests)
        self.cursors: dict[UUID, UUID | None] = {}

    async def run_once(
        self, *, worker_id: str, organization_id: UUID, at: datetime | None = None
    ) -> bool:
        at = at or datetime.now(UTC)
        async with self.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text("SELECT set_config('app.organization_id',:org,true)"),
                {"org": str(organization_id)},
            )
            members = tuple(
                await session.scalars(
                    text(
                        "SELECT m.id FROM memberships m JOIN users u ON u.id=m.user_id "
                        "WHERE m.organization_id=:org AND m.role IN ('MANAGER','ADMIN') "
                        "AND m.is_active AND u.is_active ORDER BY m.id LIMIT 10"
                    ),
                    {"org": organization_id},
                )
            )
        actor = None
        for member in members:
            actor = await self.actors.resolve(organization_id=organization_id, membership_id=member)
            if actor:
                break
        if actor is None:
            return False
        async with self.schedules.transactions(actor) as repo:
            await repo.authenticate()
            assert isinstance(repo, ScheduleRepository)
            query = select(ScheduleModel.project_id, ScheduleModel.id).where(
                ScheduleModel.organization_id == organization_id, ~ScheduleModel.paused
            )
            cursor = self.cursors.get(organization_id)
            if cursor:
                query = query.where(ScheduleModel.id > cursor)
            rows = tuple(
                (await repo.session.execute(query.order_by(ScheduleModel.id).limit(10))).all()
            )
            self.cursors[organization_id] = rows[-1][1] if rows else None
        scope = AuthorizedJobScope(actor=actor, at=at)
        processed = False
        for project_id, _ in rows:
            # Pure window resolution is passed the tick's UTC instant, not a model timestamp.
            schedules = ScheduleService(self.schedules.transactions, clock=lambda: at)
            try:
                view = await schedules.get(actor, project_id)
                if view.schedule is None or view.window is None or not view.window.enabled:
                    continue
                window = view.window
                if window.full_coverage:
                    reason = "COVERAGE"
                elif at >= window.cutoff_at:
                    reason = "CUTOFF"
                else:
                    continue
                # Existing immutable capture is checked under the same Project lock.
                try:
                    await self.digests.trigger(scope, window.id, reason)
                except ScheduleError as exc:
                    if (
                        reason == "COVERAGE"
                        and exc.code == "TRIGGER_NOT_DUE"
                        and at >= window.cutoff_at
                    ):
                        await self.digests.trigger(scope, window.id, "CUTOFF")
                    else:
                        raise
            except ScheduleError as exc:
                if exc.code not in {
                    "TRIGGER_NOT_DUE",
                    "SCHEDULE_PAUSED",
                    "WINDOW_NOT_ACTIVE",
                    "RESOURCE_NOT_FOUND",
                    "FORBIDDEN",
                    "ACTOR_INACTIVE",
                }:
                    raise
        processed = await self.jobs.run_once(scope, worker_id) or processed
        return processed
