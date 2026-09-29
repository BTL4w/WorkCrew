"""SQLAlchemy Task persistence with tenant context, audit and idempotency."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.audit.domain.events import AuditOutcome
from app.modules.identity.adapters.database_models import UserModel
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.adapters.database_models import MembershipModel
from app.modules.organization.domain.roles import MembershipRole
from app.modules.progress.adapters.completion_models import TaskCompletionCheckModel
from app.modules.progress.adapters.progress_repository import capture_project_baselines
from app.modules.progress.application.completion_policy import validate_status_completion
from app.modules.progress.domain.completion import CriterionAttestation
from app.modules.work.adapters.database_models import (
    IdempotencyRecordModel,
    IdempotencyState,
    ProjectModel,
    TaskModel,
    TaskStatusTransitionModel,
)
from app.modules.work.application.task_ports import TaskMutationResult, TaskPage, TaskRepository
from app.modules.work.domain.tasks import (
    InvalidStatusTransitionError,
    Task,
    TaskDraft,
    TaskForbiddenError,
    TaskIdempotencyKeyReusedError,
    TaskNotFoundError,
    TaskPatch,
    TaskReferenceError,
    TaskStatus,
    TaskVersionMismatchError,
)
from app.modules.work.planning.adapters.database_models import MilestoneModel, ProjectWeekModel
from app.modules.work.planning.assignment.adapters.repository import (
    validate_project_team_assignee,
)
from app.modules.work.planning.assignment.application.assignment_service import AssignmentError
from app.modules.work.planning.domain.project_weeks import ProjectWeekStatus

_TTL = timedelta(hours=24)


def _task_from_row(model: TaskModel, display_name: str | None) -> Task:
    return Task(
        id=model.id,
        organization_id=model.organization_id,
        project_id=model.project_id,
        project_week_id=model.project_week_id,
        milestone_id=model.milestone_id,
        title=model.title,
        description=model.description,
        assignee_membership_id=model.assignee_membership_id,
        assignee_display_name=display_name,
        required_skill_labels=tuple(model.required_skill_labels),
        estimated_effort_hours=model.estimated_effort_hours,
        status=model.status,
        due_date=model.due_date,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _json(task: Task) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "organization_id": str(task.organization_id),
        "project_id": str(task.project_id),
        "project_week_id": str(task.project_week_id) if task.project_week_id else None,
        "milestone_id": str(task.milestone_id) if task.milestone_id else None,
        "title": task.title,
        "description": task.description,
        "assignee_membership_id": (
            str(task.assignee_membership_id) if task.assignee_membership_id else None
        ),
        "assignee_display_name": task.assignee_display_name,
        "required_skill_labels": list(task.required_skill_labels),
        "estimated_effort_hours": task.estimated_effort_hours,
        "status": task.status.value,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "version": task.version,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def _from_json(value: dict[str, Any]) -> Task:
    due = value["due_date"]
    return Task(
        id=UUID(str(value["id"])),
        organization_id=UUID(str(value["organization_id"])),
        project_id=UUID(str(value["project_id"])),
        project_week_id=(
            UUID(str(value["project_week_id"])) if value.get("project_week_id") else None
        ),
        milestone_id=(UUID(str(value["milestone_id"])) if value.get("milestone_id") else None),
        title=str(value["title"]),
        description=str(value["description"]) if value["description"] is not None else None,
        assignee_membership_id=(
            UUID(str(value["assignee_membership_id"]))
            if value.get("assignee_membership_id")
            else None
        ),
        assignee_display_name=(
            str(value["assignee_display_name"])
            if value.get("assignee_display_name") is not None
            else None
        ),
        required_skill_labels=tuple(str(item) for item in value.get("required_skill_labels", [])),
        estimated_effort_hours=(
            int(value["estimated_effort_hours"])
            if value.get("estimated_effort_hours") is not None
            else None
        ),
        status=TaskStatus(str(value["status"])),
        due_date=date.fromisoformat(str(due)) if due else None,
        version=int(value["version"]),
        created_at=datetime.fromisoformat(str(value["created_at"])),
        updated_at=datetime.fromisoformat(str(value["updated_at"])),
    )


class SqlAlchemyTaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _activate(self, actor: AuthenticatedActor) -> None:
        await self._session.execute(text("SET LOCAL ROLE app_runtime"))
        await self._session.execute(
            text("SELECT set_config('app.organization_id', :value, true)"),
            {"value": str(actor.organization_id)},
        )
        await self._session.execute(
            text("SELECT set_config('app.membership_id', :value, true)"),
            {"value": str(actor.membership_id)},
        )

    def _base(self):
        return (
            select(TaskModel, UserModel.display_name)
            .outerjoin(
                MembershipModel,
                (MembershipModel.organization_id == TaskModel.organization_id)
                & (MembershipModel.id == TaskModel.assignee_membership_id),
            )
            .outerjoin(UserModel, UserModel.id == MembershipModel.user_id)
        )

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
    ) -> TaskPage:
        await self._activate(actor)
        predicates = [TaskModel.organization_id == actor.organization_id]
        if own_only or actor.role is MembershipRole.EMPLOYEE:
            predicates.append(TaskModel.assignee_membership_id == actor.membership_id)
        elif assignee_membership_id is not None:
            predicates.append(TaskModel.assignee_membership_id == assignee_membership_id)
        if project_id is not None:
            predicates.append(TaskModel.project_id == project_id)
        if status is not None:
            predicates.append(TaskModel.status == status)
        if due_from is not None:
            predicates.append(TaskModel.due_date >= due_from)
        if due_to is not None:
            predicates.append(TaskModel.due_date <= due_to)
        total = (
            await self._session.scalar(
                select(func.count()).select_from(TaskModel).where(*predicates)
            )
            or 0
        )
        rows = (
            await self._session.execute(
                self._base()
                .where(*predicates)
                .order_by(TaskModel.due_date.asc().nulls_last(), TaskModel.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return TaskPage(
            items=tuple(_task_from_row(model, name) for model, name in rows),
            page=page,
            page_size=page_size,
            total=total,
        )

    async def get_task(self, *, actor: AuthenticatedActor, task_id: UUID) -> Task | None:
        await self._activate(actor)
        predicates = [TaskModel.organization_id == actor.organization_id, TaskModel.id == task_id]
        if actor.role is MembershipRole.EMPLOYEE:
            predicates.append(TaskModel.assignee_membership_id == actor.membership_id)
        row = (await self._session.execute(self._base().where(*predicates))).one_or_none()
        return _task_from_row(row[0], row[1]) if row else None

    def _visible_predicates(self, actor: AuthenticatedActor) -> list[ColumnElement[bool]]:
        predicates: list[ColumnElement[bool]] = [TaskModel.organization_id == actor.organization_id]
        if actor.role is MembershipRole.EMPLOYEE:
            predicates.append(TaskModel.assignee_membership_id == actor.membership_id)
        return predicates

    async def get_next_task(self, *, actor: AuthenticatedActor) -> Task | None:
        await self._activate(actor)
        priority = case(
            (TaskModel.status == TaskStatus.IN_PROGRESS, 0),
            (TaskModel.status == TaskStatus.TO_DO, 1),
            else_=2,
        )
        due_missing = case((TaskModel.due_date.is_(None), 1), else_=0)
        row = (
            await self._session.execute(
                self._base()
                .where(
                    *self._visible_predicates(actor),
                    TaskModel.status.in_((TaskStatus.IN_PROGRESS, TaskStatus.TO_DO)),
                )
                .order_by(
                    priority,
                    due_missing,
                    TaskModel.due_date.asc(),
                    TaskModel.created_at.asc(),
                    TaskModel.id.asc(),
                )
                .limit(1)
            )
        ).one_or_none()
        return _task_from_row(row[0], row[1]) if row else None

    async def find_visible_tasks_by_title(
        self, *, actor: AuthenticatedActor, query: str, limit: int
    ) -> tuple[Task, ...]:
        await self._activate(actor)
        rows = (
            await self._session.execute(
                self._base()
                .where(*self._visible_predicates(actor), TaskModel.title.ilike(f"%{query}%"))
                .order_by(TaskModel.created_at.asc(), TaskModel.id.asc())
                .limit(min(max(limit, 1), 20))
            )
        ).all()
        return tuple(_task_from_row(model, name) for model, name in rows)

    async def _assignee_name(
        self,
        actor: AuthenticatedActor,
        membership_id: UUID | None,
    ) -> str | None:
        if membership_id is None:
            return None
        row = (
            await self._session.execute(
                select(UserModel.display_name)
                .join(MembershipModel, MembershipModel.user_id == UserModel.id)
                .where(
                    MembershipModel.organization_id == actor.organization_id,
                    MembershipModel.id == membership_id,
                    MembershipModel.is_active.is_(True),
                    UserModel.is_active.is_(True),
                )
            )
        ).one_or_none()
        if row is None:
            raise TaskReferenceError("assignee_membership_id")
        return row.display_name

    async def _validated_assignee_name(
        self,
        actor: AuthenticatedActor,
        project_id: UUID,
        membership_id: UUID | None,
    ) -> str | None:
        if membership_id is None:
            return None
        try:
            return await validate_project_team_assignee(
                self._session,
                actor=actor,
                project_id=project_id,
                assignee_membership_id=membership_id,
            )
        except AssignmentError as error:
            raise TaskReferenceError("assignee_membership_id") from error

    async def _require_project(self, actor: AuthenticatedActor, project_id: UUID) -> None:
        found = await self._session.scalar(
            select(ProjectModel.id)
            .where(
                ProjectModel.organization_id == actor.organization_id, ProjectModel.id == project_id
            )
            .with_for_update()
        )
        if found is None:
            raise TaskReferenceError("project_id")

    async def _validate_milestone(
        self,
        actor: AuthenticatedActor,
        *,
        project_id: UUID,
        milestone_id: UUID | None,
        due_date: date | None,
    ) -> None:
        if milestone_id is None:
            return
        milestone = await self._session.scalar(
            select(MilestoneModel).where(
                MilestoneModel.organization_id == actor.organization_id,
                MilestoneModel.id == milestone_id,
                MilestoneModel.project_id == project_id,
            )
        )
        if milestone is None:
            raise TaskReferenceError("milestone_id")
        if (
            due_date is not None
            and milestone.target_date is not None
            and due_date > milestone.target_date
        ):
            raise TaskReferenceError("due_date")

    async def _validate_week(
        self,
        actor: AuthenticatedActor,
        *,
        project_id: UUID,
        project_week_id: UUID | None,
        due_date: date | None,
    ) -> None:
        if project_week_id is None:
            raise TaskReferenceError("project_week_id")
        week = await self._session.scalar(
            select(ProjectWeekModel).where(
                ProjectWeekModel.organization_id == actor.organization_id,
                ProjectWeekModel.id == project_week_id,
                ProjectWeekModel.project_id == project_id,
            )
        )
        if week is None or week.status == ProjectWeekStatus.COMPLETED:
            raise TaskReferenceError("project_week_id")
        if due_date is not None and not week.start_date <= due_date <= week.end_date:
            raise TaskReferenceError("due_date")

    async def _replay(
        self, *, actor: AuthenticatedActor, operation: str, key: str, fingerprint: str
    ) -> TaskMutationResult | None:
        record = await self._session.scalar(
            select(IdempotencyRecordModel).where(
                IdempotencyRecordModel.organization_id == actor.organization_id,
                IdempotencyRecordModel.actor_membership_id == actor.membership_id,
                IdempotencyRecordModel.operation == operation,
                IdempotencyRecordModel.idempotency_key == key,
            )
        )
        if record is None:
            return None
        if (
            record.request_fingerprint != fingerprint
            or record.state is not IdempotencyState.COMPLETED
            or record.response_body is None
        ):
            raise TaskIdempotencyKeyReusedError
        return TaskMutationResult(task=_from_json(record.response_body), replayed=True)

    def _record(
        self,
        *,
        actor: AuthenticatedActor,
        operation: str,
        key: str,
        fingerprint: str,
        now: datetime,
    ) -> IdempotencyRecordModel:
        record = IdempotencyRecordModel(
            id=uuid4(),
            organization_id=actor.organization_id,
            actor_membership_id=actor.membership_id,
            operation=operation,
            idempotency_key=key,
            request_fingerprint=fingerprint,
            state=IdempotencyState.IN_PROGRESS,
            response_status=None,
            response_body=None,
            expires_at=now + _TTL,
        )
        self._session.add(record)
        return record

    def _audit(
        self,
        *,
        actor: AuthenticatedActor,
        action: str,
        task_id: UUID,
        request_id: str,
        key: str,
        before: dict[str, object],
        after: dict[str, object],
    ) -> None:
        self._session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=actor.organization_id,
                actor_membership_id=actor.membership_id,
                action=action,
                outcome=AuditOutcome.SUCCEEDED,
                resource_type="task",
                resource_id=task_id,
                request_id=request_id,
                idempotency_key=key,
                before_data=before,
                after_data=after,
                reason_data={},
            )
        )

    async def create_task(
        self,
        *,
        actor: AuthenticatedActor,
        draft: TaskDraft,
        request_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> TaskMutationResult:
        await self._activate(actor)
        operation = "task.create"
        replay = await self._replay(
            actor=actor, operation=operation, key=idempotency_key, fingerprint=request_fingerprint
        )
        if replay:
            return replay
        await self._require_project(actor, draft.project_id)
        await self._validate_milestone(
            actor,
            project_id=draft.project_id,
            milestone_id=draft.milestone_id,
            due_date=draft.due_date,
        )
        await self._validate_week(
            actor,
            project_id=draft.project_id,
            project_week_id=draft.project_week_id,
            due_date=draft.due_date,
        )
        display_name = await self._validated_assignee_name(
            actor, draft.project_id, draft.assignee_membership_id
        )
        now = datetime.now(UTC)
        record = self._record(
            actor=actor,
            operation=operation,
            key=idempotency_key,
            fingerprint=request_fingerprint,
            now=now,
        )
        model = TaskModel(
            id=uuid4(),
            organization_id=actor.organization_id,
            project_id=draft.project_id,
            project_week_id=draft.project_week_id,
            milestone_id=draft.milestone_id,
            title=draft.title,
            description=draft.description,
            assignee_membership_id=draft.assignee_membership_id,
            required_skill_labels=list(draft.required_skill_labels),
            estimated_effort_hours=draft.estimated_effort_hours,
            status=TaskStatus.TO_DO,
            due_date=draft.due_date,
            version=1,
            created_by_membership_id=actor.membership_id,
            updated_by_membership_id=actor.membership_id,
            created_at=now,
            updated_at=now,
        )
        self._session.add(model)
        await self._session.flush()
        await capture_project_baselines(
            self._session,
            actor,
            model.project_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
        task = _task_from_row(model, display_name)
        self._audit(
            actor=actor,
            action="task.created",
            task_id=task.id,
            request_id=request_id,
            key=idempotency_key,
            before={},
            after={"title": task.title, "status": task.status.value},
        )
        if task.assignee_membership_id is not None:
            self._audit(
                actor=actor,
                action="task.assigned",
                task_id=task.id,
                request_id=request_id,
                key=idempotency_key,
                before={},
                after={"assignee_membership_id": str(task.assignee_membership_id)},
            )
        record.state, record.response_status, record.response_body = (
            IdempotencyState.COMPLETED,
            201,
            _json(task),
        )
        return TaskMutationResult(task=task, replayed=False)

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
    ) -> TaskMutationResult:
        await self._activate(actor)
        operation = f"task.update:{task_id}"
        replay = await self._replay(
            actor=actor, operation=operation, key=idempotency_key, fingerprint=request_fingerprint
        )
        if replay:
            return replay
        project_id = await self._session.scalar(
            select(TaskModel.project_id).where(
                TaskModel.organization_id == actor.organization_id, TaskModel.id == task_id
            )
        )
        if project_id is None:
            raise TaskNotFoundError
        await self._require_project(actor, project_id)
        model = await self._session.scalar(
            select(TaskModel)
            .where(TaskModel.organization_id == actor.organization_id, TaskModel.id == task_id)
            .with_for_update()
        )
        if model is None:
            raise TaskNotFoundError
        if model.version != expected_version:
            raise TaskVersionMismatchError(model.version)
        display_name = (
            await self._validated_assignee_name(
                actor, model.project_id, patch.assignee_membership_id
            )
            if patch.assignee_supplied
            else await self._assignee_name(actor, model.assignee_membership_id)
        )
        await self._validate_milestone(
            actor,
            project_id=model.project_id,
            milestone_id=patch.milestone_id if patch.milestone_supplied else model.milestone_id,
            due_date=patch.due_date if patch.due_date_supplied else model.due_date,
        )
        await self._validate_week(
            actor,
            project_id=model.project_id,
            project_week_id=(
                patch.project_week_id if patch.project_week_supplied else model.project_week_id
            ),
            due_date=patch.due_date if patch.due_date_supplied else model.due_date,
        )
        now = datetime.now(UTC)
        record = self._record(
            actor=actor,
            operation=operation,
            key=idempotency_key,
            fingerprint=request_fingerprint,
            now=now,
        )
        before: dict[str, object] = {}
        after: dict[str, object] = {}
        for field, supplied, value in (
            ("title", patch.title_supplied, patch.title),
            ("description", patch.description_supplied, patch.description),
            ("assignee_membership_id", patch.assignee_supplied, patch.assignee_membership_id),
            ("due_date", patch.due_date_supplied, patch.due_date),
            ("milestone_id", patch.milestone_supplied, patch.milestone_id),
            ("project_week_id", patch.project_week_supplied, patch.project_week_id),
            (
                "required_skill_labels",
                patch.required_skill_labels_supplied,
                list(patch.required_skill_labels),
            ),
            (
                "estimated_effort_hours",
                patch.estimated_effort_hours_supplied,
                patch.estimated_effort_hours,
            ),
        ):
            if supplied:
                old = getattr(model, field)
                before[field] = str(old) if isinstance(old, (UUID, date)) else old
                setattr(model, field, value)
                after[field] = str(value) if isinstance(value, (UUID, date)) else value
        reassigned = patch.assignee_supplied and before.get("assignee_membership_id") != after.get(
            "assignee_membership_id"
        )
        model.version += 1
        model.updated_at = now
        model.updated_by_membership_id = actor.membership_id
        await self._session.flush()
        await capture_project_baselines(
            self._session,
            actor,
            model.project_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
        task = _task_from_row(model, display_name)
        self._audit(
            actor=actor,
            action="task.updated",
            task_id=task.id,
            request_id=request_id,
            key=idempotency_key,
            before=before,
            after=after,
        )
        if reassigned:
            self._audit(
                actor=actor,
                action=(
                    "task.unassigned"
                    if after["assignee_membership_id"] is None
                    else "task.assigned"
                ),
                task_id=task.id,
                request_id=request_id,
                key=idempotency_key,
                before={"assignee_membership_id": before["assignee_membership_id"]},
                after={"assignee_membership_id": after["assignee_membership_id"]},
            )
        record.state, record.response_status, record.response_body = (
            IdempotencyState.COMPLETED,
            200,
            _json(task),
        )
        return TaskMutationResult(task=task, replayed=False)

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
        attestations: tuple[CriterionAttestation, ...] = (),
    ) -> TaskMutationResult:
        await self._activate(actor)
        operation = f"task.status:{task_id}"
        replay = await self._replay(
            actor=actor, operation=operation, key=idempotency_key, fingerprint=request_fingerprint
        )
        if replay:
            return replay
        model = await self._session.scalar(
            select(TaskModel)
            .where(TaskModel.organization_id == actor.organization_id, TaskModel.id == task_id)
            .with_for_update()
        )
        if model is None:
            raise TaskNotFoundError
        if (
            actor.role is MembershipRole.EMPLOYEE
            and model.assignee_membership_id != actor.membership_id
        ):
            raise TaskForbiddenError
        if model.version != expected_version:
            raise TaskVersionMismatchError(model.version)
        if (model.status, target) not in {
            (TaskStatus.TO_DO, TaskStatus.IN_PROGRESS),
            (TaskStatus.IN_PROGRESS, TaskStatus.TO_DO),
            (TaskStatus.IN_PROGRESS, TaskStatus.DONE),
            (TaskStatus.DONE, TaskStatus.IN_PROGRESS),
        }:
            raise InvalidStatusTransitionError
        observation, _ = await validate_status_completion(
            self._session, actor, task_id, target, attestations
        )
        display_name = await self._assignee_name(actor, model.assignee_membership_id)
        now = datetime.now(UTC)
        record = self._record(
            actor=actor,
            operation=operation,
            key=idempotency_key,
            fingerprint=request_fingerprint,
            now=now,
        )
        previous = model.status
        model.status = target
        model.version += 1
        model.updated_at = now
        model.updated_by_membership_id = actor.membership_id
        transition_id = uuid4()
        self._session.add(
            TaskStatusTransitionModel(
                id=transition_id,
                organization_id=actor.organization_id,
                task_id=model.id,
                from_status=previous,
                to_status=target,
                actor_membership_id=actor.membership_id,
                task_version_after=model.version,
                occurred_at=now,
            )
        )
        await self._session.flush()
        if target is TaskStatus.DONE:
            for attestation in attestations or (None,):
                self._session.add(
                    TaskCompletionCheckModel(
                        id=uuid4(),
                        organization_id=actor.organization_id,
                        task_id=task_id,
                        transition_id=transition_id,
                        actor_membership_id=actor.membership_id,
                        task_version_after=model.version,
                        criterion_id=attestation.criterion_id if attestation else None,
                        criterion_version=attestation.version if attestation else None,
                        confirmed=True,
                        observation_id=observation.id if observation else None,
                        evidence_refs=[
                            {"evidence_id": str(ref.evidence_id), "version": ref.version}
                            for ref in attestation.evidence_refs
                        ]
                        if attestation
                        else [],
                        occurred_at=now,
                    )
                )
        await self._session.flush()
        task = _task_from_row(model, display_name)
        self._audit(
            actor=actor,
            action="task.status_changed",
            task_id=task.id,
            request_id=request_id,
            key=idempotency_key,
            before={"status": previous.value},
            after={"status": target.value},
        )
        record.state, record.response_status, record.response_body = (
            IdempotencyState.COMPLETED,
            200,
            _json(task),
        )
        return TaskMutationResult(task=task, replayed=False)

    async def audit_rejection(
        self,
        *,
        actor: AuthenticatedActor,
        action: str,
        request_id: str,
        reason_code: str,
        idempotency_key: str | None = None,
        resource_id: UUID | None = None,
    ) -> None:
        await self._activate(actor)
        self._session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=actor.organization_id,
                actor_membership_id=actor.membership_id,
                action=action,
                outcome=AuditOutcome.REJECTED,
                resource_type="task",
                resource_id=resource_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                before_data={},
                after_data={},
                reason_data={"code": reason_code},
            )
        )


class SqlAlchemyTaskTransactionFactory:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    @asynccontextmanager
    async def __call__(self) -> AsyncGenerator[TaskRepository]:
        async with self._sessions.begin() as session:
            yield SqlAlchemyTaskRepository(session)
