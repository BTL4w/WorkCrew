from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.exc import DBAPIError

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.reporting.adapters.repository import SQLReportRepository
from app.modules.reporting.adapters.transaction import ReportTransactions
from app.modules.reporting.domain.reports import ReportCaptureConflict

from .repository import SQLFeedbackRepository


class FeedbackTransactions(ReportTransactions):
    @asynccontextmanager
    async def __call__(self, actor: AuthenticatedActor) -> AsyncGenerator[SQLFeedbackRepository]:
        try:
            async with super().__call__(actor) as base:
                # Keep RR retry/error handling and tenant context owned by the existing factory.
                assert isinstance(base, SQLReportRepository)
                yield SQLFeedbackRepository(base.session, actor, self.default_timezone)
        except DBAPIError as exc:
            if (
                getattr(exc.orig, "sqlstate", None) == "23505"
                and getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
                == "uq_feedback_outcome_source"
            ):
                raise ReportCaptureConflict from exc
            raise
