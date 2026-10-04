"""Report transactions set isolation before tenant/auth/source queries."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.identity.domain.auth import AuthenticatedActor

from ..application.ports import ReportRepository
from ..domain.reports import ReportCaptureConflict
from .repository import SQLReportRepository


class ReportTransactions:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], default_timezone: str):
        self.sessions, self.default_timezone = sessions, default_timezone

    @asynccontextmanager
    async def __call__(self, actor: AuthenticatedActor) -> AsyncGenerator[ReportRepository]:
        try:
            async with self.sessions() as session, session.begin():
                await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                await session.execute(text("SET LOCAL ROLE app_runtime"))
                await session.execute(
                    text(
                        "SELECT set_config('app.organization_id',:org,true), "
                        "set_config('app.membership_id',:member,true)"
                    ),
                    {"org": str(actor.organization_id), "member": str(actor.membership_id)},
                )
                await session.execute(text("SET LOCAL statement_timeout='15s'"))
                yield SQLReportRepository(session, actor, self.default_timezone)
        except DBAPIError as exc:
            code = getattr(exc.orig, "sqlstate", None)
            diagnostic = getattr(exc.orig, "diag", None)
            replay_collision = (
                code == "23505" and getattr(diagnostic, "table_name", None) == "idempotency_records"
            )
            # A waiter may hold a pre-lock REPEATABLE READ snapshot. Retry after rollback
            # so the new transaction can see the first request's committed replay record.
            if code in {"40001", "40P01", "57014"} or replay_collision:
                raise ReportCaptureConflict from exc
            raise
