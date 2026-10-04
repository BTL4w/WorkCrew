"""Durable reservation port; all attempts are charged before execution."""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Protocol

from work_management_ai.runtime.daily_update_budget import MODEL_ATTEMPT_SCOPE

from .contracts import ReportingUsage


class ReportingUsagePort(Protocol):
    async def load(self) -> ReportingUsage: ...
    async def remaining_seconds(self) -> float: ...
    async def reserve(self, *, input_tokens: int, output_tokens: int) -> int: ...
    async def reserve_tool(self, invocation_key: str) -> None: ...
    async def consume_retry(self) -> None: ...


@contextmanager
def reporting_attempt_scope(attempt: int) -> Generator[None]:
    token = MODEL_ATTEMPT_SCOPE.set(attempt)
    try:
        yield
    finally:
        MODEL_ATTEMPT_SCOPE.reset(token)
