from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.reporting.adapters.repository import SQLReportRepository
from app.modules.reporting.adapters.transaction import ReportTransactions

from .repository import SQLFeedbackRepository


class FeedbackTransactions(ReportTransactions):
    @asynccontextmanager
    async def __call__(self, actor: AuthenticatedActor) -> AsyncGenerator[SQLFeedbackRepository]:
        async with super().__call__(actor) as base:
            # Keep RR retry/error handling and tenant context owned by the existing factory.
            assert isinstance(base, SQLReportRepository)
            yield SQLFeedbackRepository(base.session, actor, self.default_timezone)
