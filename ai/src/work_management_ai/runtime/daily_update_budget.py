"""Durable reservation ports for daily report and original-media model calls."""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID


class UsageLimitExceeded(ValueError):
    pass


@dataclass(frozen=True)
class BudgetScope:
    organization_id: UUID
    membership_id: UUID
    run_id: UUID
    evidence_versions: tuple[tuple[UUID, int], ...] = ()
    max_model_attempts: int = 3
    timeout_seconds: int = 180
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))


DAILY_MODEL_SCOPE: ContextVar[BudgetScope | None] = ContextVar("daily_model_scope", default=None)


@contextmanager
def daily_model_scope(scope: BudgetScope) -> Generator[None]:
    token = DAILY_MODEL_SCOPE.set(scope)
    try:
        yield
    finally:
        DAILY_MODEL_SCOPE.reset(token)


class UsageStore(Protocol):
    async def remaining_seconds(self, scope: BudgetScope) -> float: ...

    async def run_attempts(self, scope: BudgetScope) -> int: ...

    async def reserve(
        self, scope: BudgetScope, *, input_tokens: int, output_tokens: int
    ) -> int: ...


class MemoryUsageStore:
    def __init__(self) -> None:
        self.starts: dict[tuple[UUID, UUID, UUID], datetime] = {}
        self.counts: dict[tuple[UUID, UUID, str], tuple[int, int, int]] = {}

    async def remaining_seconds(self, scope: BudgetScope) -> float:
        key = (scope.organization_id, scope.membership_id, scope.run_id)
        start = self.starts.setdefault(key, scope.started_at)
        return max(
            0.0, min(180, scope.timeout_seconds) - (datetime.now(UTC) - start).total_seconds()
        )

    async def run_attempts(self, scope: BudgetScope) -> int:
        return self.counts.get(
            (scope.organization_id, scope.membership_id, f"run:{scope.run_id}"), (0, 0, 0)
        )[0]

    async def reserve(self, scope: BudgetScope, *, input_tokens: int, output_tokens: int) -> int:
        if input_tokens <= 0 or output_tokens <= 0:
            raise UsageLimitExceeded("INVALID_USAGE_RESERVATION")
        if await self.remaining_seconds(scope) <= 0:
            raise UsageLimitExceeded("DAILY_UPDATE_MODEL_BUDGET_EXHAUSTED")
        keys = [
            (
                (scope.organization_id, scope.membership_id, f"run:{scope.run_id}"),
                (min(3, scope.max_model_attempts), 24000, 4000),
            )
        ]
        keys += [
            (
                (scope.organization_id, scope.membership_id, f"evidence:{identifier}:{version}"),
                (10, 240000, 40000),
            )
            for identifier, version in sorted(set(scope.evidence_versions))
        ]
        for key, limit in keys:
            used = self.counts.get(key, (0, 0, 0))
            if (
                used[0] + 1 > limit[0]
                or used[1] + input_tokens > limit[1]
                or used[2] + output_tokens > limit[2]
            ):
                raise UsageLimitExceeded("DAILY_UPDATE_MODEL_BUDGET_EXHAUSTED")
        for key, _ in keys:
            attempts, inputs, outputs = self.counts.get(key, (0, 0, 0))
            self.counts[key] = (attempts + 1, inputs + input_tokens, outputs + output_tokens)
        return self.counts[(scope.organization_id, scope.membership_id, f"run:{scope.run_id}")][0]


MODEL_ATTEMPT_SCOPE: ContextVar[int | None] = ContextVar("daily_model_attempt", default=None)
