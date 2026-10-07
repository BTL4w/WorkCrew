"""Tenant-scoped PostgreSQL transaction boundary for Assistant persistence."""

import asyncio
from typing import Self
from uuid import UUID

from anyio import CancelScope
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction, async_sessionmaker

from app.core.config import get_settings
from app.modules.assistant.adapters.repository import PostgreSQLAssistantRepository
from app.modules.assistant.application.ports import AssistantRepository, AssistantTransaction
from app.modules.identity.domain.auth import AuthenticatedActor


class PostgreSQLAssistantTransaction(AssistantTransaction):
    def __init__(
        self,
        *,
        session: AsyncSession,
        organization_id: UUID,
        membership_id: UUID | None,
    ) -> None:
        self._session = session
        self._organization_id = organization_id
        self._membership_id = membership_id
        self._repository = PostgreSQLAssistantRepository(session)
        self._transaction: AsyncSessionTransaction | None = None

    @property
    def repository(self) -> AssistantRepository:
        if self._transaction is None or not self._transaction.is_active:
            raise RuntimeError("Assistant repository is unavailable outside an active transaction.")
        return self._repository

    @property
    def session(self) -> AsyncSession:
        return self._session

    async def __aenter__(self) -> Self:
        if self._transaction is not None:
            raise RuntimeError("Assistant transaction cannot be entered twice.")
        self._transaction = self._session.begin()
        try:
            await self._transaction.__aenter__()
            await self._session.execute(text("SET LOCAL ROLE app_runtime"))
            await self._session.execute(
                text("SELECT set_config('app.ai_raw_retention_days',:days,true)"),
                {"days": str(get_settings().ai_raw_context_retention_days)},
            )
            await self._session.execute(
                text("SELECT set_config('app.organization_id', :value, true)"),
                {"value": str(self._organization_id)},
            )
            if self._membership_id is not None:
                await self._session.execute(
                    text("SELECT set_config('app.membership_id', :value, true)"),
                    {"value": str(self._membership_id)},
                )
        except BaseException as error:
            with CancelScope(shield=True):
                if isinstance(error, asyncio.CancelledError):
                    await self._session.invalidate()
                else:
                    try:
                        if self._transaction.is_active:
                            await self._transaction.rollback()
                    finally:
                        await self._session.close()
            raise
        return self

    async def commit(self) -> None:
        if self._transaction is None:
            raise RuntimeError("Assistant transaction is not active.")
        await self._transaction.commit()

    async def rollback(self) -> None:
        if self._transaction is None:
            raise RuntimeError("Assistant transaction is not active.")
        await self._transaction.rollback()

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        if self._transaction is None:
            raise RuntimeError("Assistant transaction was not entered.")
        with CancelScope(shield=True):
            if isinstance(exc_val, asyncio.CancelledError):
                await self._session.invalidate()
            else:
                try:
                    await self._transaction.__aexit__(exc_type, exc_val, exc_tb)
                finally:
                    await self._session.close()


class PostgreSQLAssistantTransactionFactory:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    def __call__(self, context: AuthenticatedActor | UUID) -> PostgreSQLAssistantTransaction:
        session = self._session_factory(close_resets_only=False)
        if isinstance(context, AuthenticatedActor):
            return PostgreSQLAssistantTransaction(
                session=session,
                organization_id=context.organization_id,
                membership_id=context.membership_id,
            )
        return PostgreSQLAssistantTransaction(
            session=session,
            organization_id=context,
            membership_id=None,
        )
