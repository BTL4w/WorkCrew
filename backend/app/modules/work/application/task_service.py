"""Task authorization, validation, fingerprints, and transaction boundaries."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.progress.domain.completion import CriterionAttestation
from app.modules.work.application.shared_commands import build_task_draft
from app.modules.work.application.task_ports import (
    TaskMutationResult,
    TaskPage,
    TaskTransactionFactory,
)
from app.modules.work.domain.tasks import (
    Task,
    TaskError,
    TaskForbiddenError,
    TaskNotFoundError,
    TaskPatch,
    TaskStatus,
)

_WRITERS = frozenset({MembershipRole.ADMIN, MembershipRole.MANAGER})


def _fingerprint(operation: str, values: dict[str, object]) -> str:
    canonical = json.dumps(
        {"operation": operation, "values": values},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class TaskService:
    def __init__(self, transaction_factory: TaskTransactionFactory) -> None:
        self._transactions = transaction_factory

    async def list_tasks(
        self,
        *,
        actor: AuthenticatedActor,
        project_id: UUID | None,
        assignee_membership_id: UUID | None,
        status: TaskStatus | None,
        page: int,
        page_size: int,
    ) -> TaskPage:
        async with self._transactions() as repository:
            return await repository.list_tasks(
                actor=actor,
                project_id=project_id,
                assignee_membership_id=assignee_membership_id,
                status=status,
                due_from=None,
                due_to=None,
                own_only=False,
                page=page,
                page_size=page_size,
            )

    async def my_tasks(
        self,
        *,
        actor: AuthenticatedActor,
        status: TaskStatus | None,
        due_from: date | None,
        due_to: date | None,
        page: int,
        page_size: int,
    ) -> TaskPage:
        async with self._transactions() as repository:
            return await repository.list_tasks(
                actor=actor,
                project_id=None,
                assignee_membership_id=None,
                status=status,
                due_from=due_from,
                due_to=due_to,
                own_only=True,
                page=page,
                page_size=page_size,
            )

    async def get_task(self, *, actor: AuthenticatedActor, task_id: UUID) -> Task:
        async with self._transactions() as repository:
            task = await repository.get_task(actor=actor, task_id=task_id)
        if task is None:
            raise TaskNotFoundError
        return task

    async def get_next_task(self, *, actor: AuthenticatedActor) -> Task | None:
        """Return the actor-visible next task using repository-owned SQL ordering."""
        async with self._transactions() as repository:
            return await repository.get_next_task(actor=actor)

    async def find_visible_tasks_by_title(
        self, *, actor: AuthenticatedActor, query: str, limit: int = 20
    ) -> tuple[Task, ...]:
        normalized = query.strip()
        if not normalized:
            return ()
        async with self._transactions() as repository:
            return await repository.find_visible_tasks_by_title(
                actor=actor, query=normalized, limit=min(max(limit, 1), 20)
            )

    async def _require_writer(
        self,
        *,
        actor: AuthenticatedActor,
        action: str,
        request_id: str,
        idempotency_key: str,
        resource_id: UUID | None = None,
    ) -> None:
        if actor.role in _WRITERS:
            return
        async with self._transactions() as repository:
            await repository.audit_rejection(
                actor=actor,
                action=action,
                request_id=request_id,
                reason_code="FORBIDDEN",
                idempotency_key=idempotency_key,
                resource_id=resource_id,
            )
        raise TaskForbiddenError

    async def _audit_error(
        self,
        *,
        actor: AuthenticatedActor,
        action: str,
        request_id: str,
        idempotency_key: str,
        resource_id: UUID | None,
        error: TaskError,
    ) -> None:
        async with self._transactions() as repository:
            await repository.audit_rejection(
                actor=actor,
                action=action,
                request_id=request_id,
                reason_code=type(error).__name__,
                idempotency_key=idempotency_key,
                resource_id=resource_id,
            )

    async def create_task(
        self,
        *,
        actor: AuthenticatedActor,
        project_id: UUID,
        project_week_id: UUID,
        title: str,
        description: str | None,
        assignee_membership_id: UUID | None,
        required_skill_labels: tuple[str, ...],
        estimated_effort_hours: int,
        due_date: date | None,
        request_id: str,
        idempotency_key: str,
        milestone_id: UUID | None = None,
    ) -> TaskMutationResult:
        await self._require_writer(
            actor=actor,
            action="task.created",
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
        try:
            draft = build_task_draft(
                project_id=project_id,
                project_week_id=project_week_id,
                milestone_id=milestone_id,
                title=title,
                description=description,
                assignee_membership_id=assignee_membership_id,
                required_skill_labels=required_skill_labels,
                estimated_effort_hours=estimated_effort_hours,
                due_date=due_date,
            )
            fingerprint = _fingerprint(
                "task.create",
                {
                    "project_id": project_id,
                    "project_week_id": project_week_id,
                    "title": draft.title,
                    "description": draft.description,
                    "assignee_membership_id": assignee_membership_id,
                    "required_skill_labels": draft.required_skill_labels,
                    "estimated_effort_hours": draft.estimated_effort_hours,
                    "due_date": due_date,
                    "milestone_id": milestone_id,
                },
            )
            async with self._transactions() as repository:
                return await repository.create_task(
                    actor=actor,
                    draft=draft,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=fingerprint,
                )
        except TaskError as error:
            await self._audit_error(
                actor=actor,
                action="task.created",
                request_id=request_id,
                idempotency_key=idempotency_key,
                resource_id=None,
                error=error,
            )
            raise

    async def update_task(
        self,
        *,
        actor: AuthenticatedActor,
        task_id: UUID,
        title: str | None,
        title_supplied: bool,
        description: str | None,
        description_supplied: bool,
        assignee_membership_id: UUID | None,
        assignee_supplied: bool,
        due_date: date | None,
        due_date_supplied: bool,
        expected_version: int,
        request_id: str,
        idempotency_key: str,
        milestone_id: UUID | None = None,
        milestone_supplied: bool = False,
        project_week_id: UUID | None = None,
        project_week_supplied: bool = False,
        required_skill_labels: tuple[str, ...] = (),
        required_skill_labels_supplied: bool = False,
        estimated_effort_hours: int | None = None,
        estimated_effort_hours_supplied: bool = False,
    ) -> TaskMutationResult:
        await self._require_writer(
            actor=actor,
            action="task.updated",
            request_id=request_id,
            idempotency_key=idempotency_key,
            resource_id=task_id,
        )
        try:
            patch = TaskPatch.create(
                title=title,
                title_supplied=title_supplied,
                description=description,
                description_supplied=description_supplied,
                assignee_membership_id=assignee_membership_id,
                assignee_supplied=assignee_supplied,
                due_date=due_date,
                due_date_supplied=due_date_supplied,
                milestone_id=milestone_id,
                milestone_supplied=milestone_supplied,
                project_week_id=project_week_id,
                project_week_supplied=project_week_supplied,
                required_skill_labels=required_skill_labels,
                required_skill_labels_supplied=required_skill_labels_supplied,
                estimated_effort_hours=estimated_effort_hours,
                estimated_effort_hours_supplied=estimated_effort_hours_supplied,
            )
            patch.validate_not_empty()
            fingerprint = _fingerprint(
                "task.update",
                {
                    "task_id": task_id,
                    "title": patch.title if patch.title_supplied else "__omitted__",
                    "description": patch.description
                    if patch.description_supplied
                    else "__omitted__",
                    "assignee": patch.assignee_membership_id
                    if patch.assignee_supplied
                    else "__omitted__",
                    "due_date": patch.due_date if patch.due_date_supplied else "__omitted__",
                    "milestone_id": (
                        patch.milestone_id if patch.milestone_supplied else "__omitted__"
                    ),
                    "project_week_id": (
                        patch.project_week_id if patch.project_week_supplied else "__omitted__"
                    ),
                    "required_skill_labels": (
                        patch.required_skill_labels
                        if patch.required_skill_labels_supplied
                        else "__omitted__"
                    ),
                    "estimated_effort_hours": (
                        patch.estimated_effort_hours
                        if patch.estimated_effort_hours_supplied
                        else "__omitted__"
                    ),
                    "expected_version": expected_version,
                },
            )
            async with self._transactions() as repository:
                return await repository.update_task(
                    actor=actor,
                    task_id=task_id,
                    patch=patch,
                    expected_version=expected_version,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=fingerprint,
                )
        except TaskError as error:
            await self._audit_error(
                actor=actor,
                action="task.updated",
                request_id=request_id,
                idempotency_key=idempotency_key,
                resource_id=task_id,
                error=error,
            )
            raise

    async def transition_task(
        self,
        *,
        actor: AuthenticatedActor,
        task_id: UUID,
        target: TaskStatus,
        expected_version: int,
        request_id: str,
        idempotency_key: str,
        attestations: tuple[CriterionAttestation, ...] = (),
    ) -> TaskMutationResult:
        values: dict[str, object] = {
            "task_id": task_id,
            "target": target,
            "expected_version": expected_version,
        }
        if attestations:
            values["attestations"] = [
                {
                    "criterion_id": str(item.criterion_id),
                    "version": item.version,
                    "confirmed": item.confirmed,
                    "evidence_refs": [
                        {"evidence_id": str(ref.evidence_id), "version": ref.version}
                        for ref in item.evidence_refs
                    ],
                }
                for item in attestations
            ]
        fingerprint = _fingerprint("task.status", values)
        try:
            async with self._transactions() as repository:
                return await repository.transition_task(
                    actor=actor,
                    task_id=task_id,
                    target=target,
                    expected_version=expected_version,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=fingerprint,
                    attestations=attestations,
                )
        except TaskError as error:
            await self._audit_error(
                actor=actor,
                action="task.status_changed",
                request_id=request_id,
                idempotency_key=idempotency_key,
                resource_id=task_id,
                error=error,
            )
            raise
