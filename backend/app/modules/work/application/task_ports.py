"""Typed persistence ports for Task use cases."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date
from typing import Protocol
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.progress.domain.completion import CriterionAttestation
from app.modules.work.domain.tasks import Task, TaskDraft, TaskPatch, TaskStatus


@dataclass(frozen=True, slots=True)
class TaskPage:
    items: tuple[Task, ...]
    page: int
    page_size: int
    total: int


@dataclass(frozen=True, slots=True)
class TaskMutationResult:
    task: Task
    replayed: bool


class TaskRepository(Protocol):
    async def get_next_task(self, *, actor: AuthenticatedActor) -> Task | None: ...
    async def find_visible_tasks_by_title(
        self, *, actor: AuthenticatedActor, query: str, limit: int
    ) -> tuple[Task, ...]: ...
    async def list_tasks(
        self,
        *,
        actor: AuthenticatedActor,
        project_id: UUID | None,
        assignee_membership_id: UUID | None,
        status: TaskStatus | None,
        due_from: date | None,
        due_to: date | None,
        own_only: bool,
        page: int,
        page_size: int,
    ) -> TaskPage: ...
    async def get_task(self, *, actor: AuthenticatedActor, task_id: UUID) -> Task | None: ...
    async def create_task(
        self,
        *,
        actor: AuthenticatedActor,
        draft: TaskDraft,
        request_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> TaskMutationResult: ...
    async def update_task(
        self,
        *,
        actor: AuthenticatedActor,
        task_id: UUID,
        patch: TaskPatch,
        expected_version: int,
        request_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> TaskMutationResult: ...
    async def transition_task(
        self,
        *,
        actor: AuthenticatedActor,
        task_id: UUID,
        target: TaskStatus,
        expected_version: int,
        request_id: str,
        idempotency_key: str,
        request_fingerprint: str,
        attestations: tuple[CriterionAttestation, ...],
    ) -> TaskMutationResult: ...
    async def audit_rejection(
        self,
        *,
        actor: AuthenticatedActor,
        action: str,
        request_id: str,
        reason_code: str,
        idempotency_key: str | None = None,
        resource_id: UUID | None = None,
    ) -> None: ...


TaskTransactionFactory = Callable[[], AbstractAsyncContextManager[TaskRepository]]
