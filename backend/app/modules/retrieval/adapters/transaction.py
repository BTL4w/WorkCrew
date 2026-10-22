"""Short owner-scoped transactions and trusted configuration activation."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

from anyio import CancelScope
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.retrieval.application.ports import RetrievalRepository
from app.modules.retrieval.domain.models import RetrievalPolicyError
from work_management_ai.runtime.activation import ActivationContext, capability_enabled


class RetrievalActivationPolicy:
    def __init__(self, settings: Callable[[], Settings]) -> None:
        self._settings = settings

    def ensure_enabled(self) -> None:
        settings = self._settings()
        context = ActivationContext(
            active_phase=5,
            core_exit_verified=settings.retrieval_core_exit_verified,
            active_tracks=frozenset({"I3"}) if settings.retrieval_enabled else frozenset(),
            provider_ready=settings.retrieval_provider == "mock"
            or (
                settings.retrieval_provider == "exa"
                and settings.exa_api_key is not None
                and bool(settings.exa_api_key.get_secret_value().strip())
            ),
        )
        if not capability_enabled("retrieval.search_web", context):
            raise RetrievalPolicyError("RETRIEVAL_CAPABILITY_INACTIVE")


class PostgreSQLRetrievalTransactions:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @asynccontextmanager
    async def __call__(self, actor: AuthenticatedActor) -> AsyncGenerator[RetrievalRepository]:
        from app.modules.retrieval.adapters.repository import PostgreSQLRetrievalRepository

        session = self._sessions(close_resets_only=False)
        try:
            async with session.begin():
                await session.execute(text("SET LOCAL ROLE app_runtime"))
                await session.execute(text("SET LOCAL statement_timeout='8s'"))
                await session.execute(
                    text(
                        "SELECT set_config('app.organization_id',:org,true),set_config('a"
                        "pp.membership_id',:member,true),set_config('app.ai_raw_retention"
                        "_days',:days,true)"
                    ),
                    {
                        "org": str(actor.organization_id),
                        "member": str(actor.membership_id),
                        "days": str(get_settings().ai_raw_context_retention_days),
                    },
                )
                yield PostgreSQLRetrievalRepository(session, actor)
        except asyncio.CancelledError:
            with CancelScope(shield=True):
                await session.invalidate()
            raise
        finally:
            with CancelScope(shield=True):
                await session.close()
