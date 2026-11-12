"""Read-only repeatable snapshots for source-versioned workload caching."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.optimization.adapters.generation import read_actor_revision
from app.modules.optimization.adapters.redis_runtime import RedisRuntime
from app.modules.optimization.application.cache_contracts import CacheRevision
from app.modules.optimization.application.single_flight import SingleFlight
from app.modules.people_capacity.adapters.repository import SqlAlchemyPeopleCapacityRepository
from app.modules.people_capacity.application.workload_cache import (
    WorkloadCacheReader,
    WorkloadReadSnapshot,
)
from app.modules.people_capacity.application.workload_service import WorkloadService
from app.modules.people_capacity.domain.skills import PeopleSkillForbiddenError
from app.modules.people_capacity.domain.workload import WeeklyWorkload


@dataclass
class _Snapshot:
    actor: AuthenticatedActor
    revision: CacheRevision | None
    repository: SqlAlchemyPeopleCapacityRepository

    async def load(
        self, *, week_start: date, membership_id: UUID | None
    ) -> tuple[WeeklyWorkload, ...]:
        return await WorkloadService(self.repository).list_weekly_workload(
            actor=self.actor,
            week_start=week_start,
            membership_id=membership_id,
        )


class SqlAlchemyWorkloadReadTransactions:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @asynccontextmanager
    async def __call__(self, actor: AuthenticatedActor) -> AsyncGenerator[WorkloadReadSnapshot]:
        async with self._sessions() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            await session.execute(
                text(
                    "SELECT set_config('role','app_runtime',true), "
                    "set_config('app.organization_id',:org,true), "
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            resolved = await read_actor_revision(session, actor)
            if resolved is None:
                raise PeopleSkillForbiddenError
            current, revision = resolved
            yield _Snapshot(current, revision, SqlAlchemyPeopleCapacityRepository(session))


def build_workload_cache(
    settings: Settings, sessions: async_sessionmaker[AsyncSession], runtime: RedisRuntime
) -> WorkloadCacheReader | None:
    if not settings.read_cache_enabled or "workload.weekly" not in settings.read_cache_families:
        return None
    return WorkloadCacheReader(
        transactions=SqlAlchemyWorkloadReadTransactions(sessions),
        cache=runtime.read_cache,
        namespace=runtime.namespace,
        single_flight=SingleFlight(runtime.leases) if runtime.leases is not None else None,
    )
