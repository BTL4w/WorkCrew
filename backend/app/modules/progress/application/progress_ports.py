"""Typed read boundary; report confirmation will supply observations in Task 3."""

from contextlib import AbstractAsyncContextManager
from typing import Protocol
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.progress.domain.weekly_progress import PlanBaseline, TaskActual


class ProgressRepository(Protocol):
    async def get(
        self, project_id: UUID, week_id: UUID
    ) -> tuple[PlanBaseline, PlanBaseline, tuple[TaskActual, ...]] | None: ...


class ProgressTransactionFactory(Protocol):
    def __call__(
        self, actor: AuthenticatedActor
    ) -> AbstractAsyncContextManager[ProgressRepository]: ...
