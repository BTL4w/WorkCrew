"""Transaction boundary implementation for AI planning runs."""

from typing import Self
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction, async_sessionmaker

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.planning_runs.adapters.repository import PostgreSQLPlanningRunRepository
from app.modules.planning_runs.application.ports import (
    PlanningRunRepository,
    PlanningRunTransaction,
)


class PostgreSQLPlanningRunTransaction(PlanningRunTransaction):
    """Async PostgreSQL transaction manager enforcing RLS tenant context and app_runtime role."""

    def __init__(
        self,
        session: AsyncSession,
        organization_id: UUID,
        membership_id: UUID | None = None,
        reporting_timezone: str = "UTC",
    ) -> None:
        self._session = session
        self._organization_id = organization_id
        self._membership_id = membership_id
        self._repository = PostgreSQLPlanningRunRepository(session, reporting_timezone)
        self._transaction: AsyncSessionTransaction | None = None

    @property
    def repository(self) -> PlanningRunRepository:
        if self._transaction is None or not self._transaction.is_active:
            raise RuntimeError(
                "Planning run repository is unavailable outside an active transaction."
            )
        return self._repository

    @property
    def session(self) -> AsyncSession:
        return self._session

    async def commit(self) -> None:
        if self._transaction is None:
            raise RuntimeError("Planning run transaction is not active.")
        await self._transaction.commit()

    async def rollback(self) -> None:
        if self._transaction is None:
            raise RuntimeError("Planning run transaction is not active.")
        await self._transaction.rollback()

    async def __aenter__(self) -> Self:
        if self._transaction is not None:
            raise RuntimeError("Planning run transaction cannot be entered more than once.")
        self._transaction = self._session.begin()
        await self._transaction.__aenter__()
        await self._session.execute(text("SET LOCAL ROLE app_runtime"))
        await self._session.execute(
            text("SELECT set_config('app.organization_id', :org_id, true)"),
            {"org_id": str(self._organization_id)},
        )
        if self._membership_id:
            await self._session.execute(
                text("SELECT set_config('app.membership_id', :mem_id, true)"),
                {"mem_id": str(self._membership_id)},
            )
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc_val: object,
        exc_tb: object,
    ) -> None:
        transaction = self._transaction
        if transaction is None:
            raise RuntimeError("Planning run transaction was not entered.")
        try:
            await transaction.__aexit__(exc_type, exc_val, exc_tb)
        finally:
            await self._session.close()


class PostgreSQLPlanningRunTransactionFactory:
    """Factory creating tenant-scoped planning run transactions."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], reporting_timezone: str = "UTC"
    ) -> None:
        self._reporting_timezone = reporting_timezone
        self._session_factory = session_factory

    def __call__(self, context: AuthenticatedActor | UUID) -> PlanningRunTransaction:
        session = self._session_factory(close_resets_only=False)
        if isinstance(context, AuthenticatedActor):
            return PostgreSQLPlanningRunTransaction(
                session=session,
                organization_id=context.organization_id,
                membership_id=context.membership_id,
                reporting_timezone=self._reporting_timezone,
            )
        return PostgreSQLPlanningRunTransaction(
            session=session,
            organization_id=context,
            reporting_timezone=self._reporting_timezone,
        )
