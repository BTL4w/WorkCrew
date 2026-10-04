"""Fenced, transactional reservations survive crashes and lease transfers."""

from datetime import datetime
from typing import cast

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from work_management_ai.agents.reporting.contracts import ReportingUsage, ReportingUsageScope

from ..domain.reports import ReportError
from .generation_repository import GenerationTransactions, SQLGenerationRepository
from .usage_models import ReportGenerationUsageModel


class ReportingUsageStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self.transactions = GenerationTransactions(sessions, "UTC")

    async def _row(
        self, repo: SQLGenerationRepository, scope: ReportingUsageScope
    ) -> ReportGenerationUsageModel:
        try:
            await repo.job(scope)
        except ReportError as exc:
            raise ValueError(exc.code) from exc
        row = await repo.session.scalar(
            select(ReportGenerationUsageModel)
            .where(
                ReportGenerationUsageModel.organization_id == scope.organization_id,
                ReportGenerationUsageModel.generation_id == scope.generation_id,
            )
            .with_for_update()
        )
        if row is None:
            raise ValueError("REPORTING_USAGE_MISSING")
        return row

    async def load(self, scope: ReportingUsageScope) -> ReportingUsage:
        async with self.transactions(scope.organization_id) as port:
            row = await self._row(cast(SQLGenerationRepository, port), scope)
            return ReportingUsage.model_validate(
                {key: getattr(row, key) for key in ReportingUsage.model_fields}
            )

    async def remaining_seconds(self, scope: ReportingUsageScope) -> float:
        async with self.transactions(scope.organization_id) as port:
            repo = cast(SQLGenerationRepository, port)
            await self._row(repo, scope)
            job = await repo.job(scope)
            at = cast(datetime, await repo.session.scalar(text("SELECT clock_timestamp()")))
            assert job.deadline is not None
            return max(0.0, (job.deadline - at).total_seconds())

    async def reserve(
        self, scope: ReportingUsageScope, *, input_tokens: int, output_tokens: int
    ) -> int:
        if (
            type(input_tokens) is not int
            or type(output_tokens) is not int
            or min(input_tokens, output_tokens) < 1
        ):
            raise ValueError("REPORTING_RESERVATION_INVALID")
        async with self.transactions(scope.organization_id) as port:
            row = await self._row(cast(SQLGenerationRepository, port), scope)
            if (
                row.attempts >= 3
                or row.input_reserved + input_tokens > 48000
                or row.output_reserved + output_tokens > 8000
            ):
                raise ValueError("REPORTING_MODEL_BUDGET")
            row.attempts += 1
            row.input_reserved += input_tokens
            row.output_reserved += output_tokens
            row.reservations = {
                **row.reservations,
                f"model:{row.attempts}": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
            }
            return row.attempts

    async def reserve_tool(self, scope: ReportingUsageScope, invocation_key: str) -> None:
        async with self.transactions(scope.organization_id) as port:
            row = await self._row(cast(SQLGenerationRepository, port), scope)
            if row.tools >= 6:
                raise ValueError("REPORTING_TOOL_BUDGET")
            row.tools += 1
            row.reservations = {**row.reservations, str(row.tools): invocation_key}

    async def consume_retry(self, scope: ReportingUsageScope) -> None:
        async with self.transactions(scope.organization_id) as port:
            row = await self._row(cast(SQLGenerationRepository, port), scope)
            if row.retries >= 1:
                raise ValueError("REPORTING_RETRY_BUDGET")
            row.retries += 1


class ScopedReportingUsage:
    def __init__(self, store: ReportingUsageStore, scope: ReportingUsageScope):
        self.store, self.scope = store, scope

    async def load(self) -> ReportingUsage:
        return await self.store.load(self.scope)

    async def remaining_seconds(self) -> float:
        return await self.store.remaining_seconds(self.scope)

    async def reserve(self, *, input_tokens: int, output_tokens: int) -> int:
        return await self.store.reserve(
            self.scope, input_tokens=input_tokens, output_tokens=output_tokens
        )

    async def reserve_tool(self, invocation_key: str) -> None:
        await self.store.reserve_tool(self.scope, invocation_key)

    async def consume_retry(self) -> None:
        await self.store.consume_retry(self.scope)
