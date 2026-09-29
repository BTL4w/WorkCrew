"""SQLAlchemy persistence for audited, tenant-scoped manual planning CRUD."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.audit.domain.events import AuditOutcome
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.progress.adapters.progress_models import WeeklyPlanBaselineModel
from app.modules.progress.adapters.progress_repository import capture_project_baselines
from app.modules.work.adapters.database_models import (
    IdempotencyRecordModel,
    IdempotencyState,
    ProjectModel,
    TaskModel,
)
from app.modules.work.planning.adapters.database_models import (
    AcceptanceCriterionModel,
    GoalModel,
    MilestoneModel,
    ProjectWeekModel,
    TaskDependencyModel,
)
from app.modules.work.planning.application.manual_ports import (
    ManualPlanningRepository,
    PlanningDeleteResult,
    PlanningMutationResult,
    PlanningPage,
    PlanningResource,
)
from app.modules.work.planning.application.manual_service import (
    CrossProjectDependencyError,
    DependencyCycleError,
    DuplicateAcceptanceCriterionError,
    DuplicateDependencyError,
    GoalAlreadyExistsError,
    MilestoneDateInvariantError,
    PlanningIdempotencyKeyReusedError,
    PlanningNotFoundError,
    PlanningReferenceError,
    PlanningVersionMismatchError,
    ProjectWeekDeleteBlockedError,
    ProjectWeekOverlapError,
)
from app.modules.work.planning.domain.acceptance_criteria import (
    AcceptanceCriterion,
    AcceptanceCriterionDraft,
    AcceptanceCriterionPatch,
)
from app.modules.work.planning.domain.dependencies import (
    TaskDependency,
    TaskDependencyDraft,
    TaskDependencyPatch,
)
from app.modules.work.planning.domain.goals import Goal, GoalDraft, GoalPatch
from app.modules.work.planning.domain.milestones import (
    Milestone,
    MilestoneDraft,
    MilestonePatch,
)
from app.modules.work.planning.domain.project_weeks import (
    ProjectWeek,
    ProjectWeekDraft,
    ProjectWeekPatch,
    ProjectWeekStatus,
)

_TTL = timedelta(hours=24)


def _goal(model: GoalModel) -> Goal:
    return Goal(
        id=model.id,
        organization_id=model.organization_id,
        project_id=model.project_id,
        title=model.title,
        description=model.description,
        expected_outcomes=tuple(model.expected_outcomes),
        target_date=model.target_date,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _milestone(model: MilestoneModel) -> Milestone:
    return Milestone(
        id=model.id,
        organization_id=model.organization_id,
        project_id=model.project_id,
        name=model.name,
        description=model.description,
        target_date=model.target_date,
        position=model.position,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _project_week(model: ProjectWeekModel) -> ProjectWeek:
    return ProjectWeek(
        id=model.id,
        organization_id=model.organization_id,
        project_id=model.project_id,
        week_number=model.week_number,
        start_date=model.start_date,
        end_date=model.end_date,
        objective=model.objective,
        status=ProjectWeekStatus(model.status),
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _dependency(model: TaskDependencyModel) -> TaskDependency:
    return TaskDependency(
        id=model.id,
        organization_id=model.organization_id,
        predecessor_task_id=model.predecessor_task_id,
        successor_task_id=model.successor_task_id,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _criterion(model: AcceptanceCriterionModel) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=model.id,
        organization_id=model.organization_id,
        task_id=model.task_id,
        text=model.text,
        position=model.position,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _json(resource: PlanningResource) -> dict[str, Any]:
    common = {
        "id": str(resource.id),
        "organization_id": str(resource.organization_id),
        "version": resource.version,
        "created_at": resource.created_at.isoformat(),
        "updated_at": resource.updated_at.isoformat(),
    }
    if isinstance(resource, Goal):
        return {
            **common,
            "kind": "goal",
            "project_id": str(resource.project_id),
            "title": resource.title,
            "description": resource.description,
            "expected_outcomes": list(resource.expected_outcomes),
            "target_date": resource.target_date.isoformat() if resource.target_date else None,
        }
    if isinstance(resource, Milestone):
        return {
            **common,
            "kind": "milestone",
            "project_id": str(resource.project_id),
            "name": resource.name,
            "description": resource.description,
            "target_date": resource.target_date.isoformat() if resource.target_date else None,
            "position": resource.position,
        }
    if isinstance(resource, ProjectWeek):
        return {
            **common,
            "kind": "project_week",
            "project_id": str(resource.project_id),
            "week_number": resource.week_number,
            "start_date": resource.start_date.isoformat(),
            "end_date": resource.end_date.isoformat(),
            "objective": resource.objective,
            "status": resource.status.value,
        }
    if isinstance(resource, TaskDependency):
        return {
            **common,
            "kind": "dependency",
            "predecessor_task_id": str(resource.predecessor_task_id),
            "successor_task_id": str(resource.successor_task_id),
        }
    return {
        **common,
        "kind": "criterion",
        "task_id": str(resource.task_id),
        "text": resource.text,
        "position": resource.position,
    }


def _resource(value: dict[str, Any]) -> PlanningResource:
    resource_id = UUID(str(value["id"]))
    organization_id = UUID(str(value["organization_id"]))
    version = int(value["version"])
    created_at = datetime.fromisoformat(str(value["created_at"]))
    updated_at = datetime.fromisoformat(str(value["updated_at"]))
    kind = value["kind"]
    target = date.fromisoformat(str(value["target_date"])) if value.get("target_date") else None
    if kind == "goal":
        return Goal(
            id=resource_id,
            organization_id=organization_id,
            version=version,
            created_at=created_at,
            updated_at=updated_at,
            project_id=UUID(str(value["project_id"])),
            title=str(value["title"]),
            description=str(value["description"]) if value["description"] is not None else None,
            expected_outcomes=tuple(str(item) for item in value["expected_outcomes"]),
            target_date=target,
        )
    if kind == "milestone":
        return Milestone(
            id=resource_id,
            organization_id=organization_id,
            version=version,
            created_at=created_at,
            updated_at=updated_at,
            project_id=UUID(str(value["project_id"])),
            name=str(value["name"]),
            description=str(value["description"]) if value["description"] is not None else None,
            target_date=target,
            position=int(value["position"]),
        )
    if kind == "project_week":
        return ProjectWeek(
            id=resource_id,
            organization_id=organization_id,
            version=version,
            created_at=created_at,
            updated_at=updated_at,
            project_id=UUID(str(value["project_id"])),
            week_number=int(value["week_number"]),
            start_date=date.fromisoformat(str(value["start_date"])),
            end_date=date.fromisoformat(str(value["end_date"])),
            objective=str(value["objective"]),
            status=ProjectWeekStatus(str(value["status"])),
        )
    if kind == "dependency":
        return TaskDependency(
            id=resource_id,
            organization_id=organization_id,
            version=version,
            created_at=created_at,
            updated_at=updated_at,
            predecessor_task_id=UUID(str(value["predecessor_task_id"])),
            successor_task_id=UUID(str(value["successor_task_id"])),
        )
    return AcceptanceCriterion(
        id=resource_id,
        organization_id=organization_id,
        version=version,
        created_at=created_at,
        updated_at=updated_at,
        task_id=UUID(str(value["task_id"])),
        text=str(value["text"]),
        position=int(value["position"]),
    )


class SqlAlchemyManualPlanningRepository:
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

    def _visible_project(self, actor: AuthenticatedActor, project_column: Any) -> Any:
        if actor.role is not MembershipRole.EMPLOYEE:
            return project_column.is_not(None)
        return (
            select(TaskModel.id)
            .where(
                TaskModel.organization_id == actor.organization_id,
                TaskModel.project_id == project_column,
                TaskModel.assignee_membership_id == actor.membership_id,
            )
            .exists()
        )

    async def _project(self, actor: AuthenticatedActor, project_id: UUID) -> None:
        found = await self._session.scalar(
            select(ProjectModel.id).where(
                ProjectModel.organization_id == actor.organization_id, ProjectModel.id == project_id
            )
        )
        if found is None:
            raise PlanningReferenceError("project_id")

    async def _lock_project(self, actor: AuthenticatedActor, project_id: UUID) -> None:
        found = await self._session.scalar(
            select(ProjectModel.id)
            .where(
                ProjectModel.organization_id == actor.organization_id,
                ProjectModel.id == project_id,
            )
            .with_for_update()
        )
        if found is None:
            raise PlanningReferenceError("project_id")

    async def _ensure_week_range_available(
        self,
        actor: AuthenticatedActor,
        *,
        project_id: UUID,
        start_date: date,
        end_date: date,
        exclude_id: UUID | None = None,
    ) -> None:
        query = select(ProjectWeekModel.id).where(
            ProjectWeekModel.organization_id == actor.organization_id,
            ProjectWeekModel.project_id == project_id,
            ProjectWeekModel.start_date <= end_date,
            ProjectWeekModel.end_date >= start_date,
        )
        if exclude_id is not None:
            query = query.where(ProjectWeekModel.id != exclude_id)
        if await self._session.scalar(query.limit(1)) is not None:
            raise ProjectWeekOverlapError

    async def _task(self, actor: AuthenticatedActor, task_id: UUID) -> TaskModel:
        model = await self._session.scalar(
            select(TaskModel).where(
                TaskModel.organization_id == actor.organization_id, TaskModel.id == task_id
            )
        )
        if model is None:
            raise PlanningReferenceError("task_id")
        return model

    async def _replay(
        self, actor: AuthenticatedActor, operation: str, key: str, fingerprint: str
    ) -> PlanningMutationResult | PlanningDeleteResult | None:
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
            raise PlanningIdempotencyKeyReusedError
        if record.response_body.get("kind") == "deleted":
            return PlanningDeleteResult(
                resource_id=UUID(str(record.response_body["id"])),
                version=int(record.response_body["version"]),
                replayed=True,
            )
        return PlanningMutationResult(resource=_resource(record.response_body), replayed=True)

    def _record(
        self, actor: AuthenticatedActor, operation: str, key: str, fingerprint: str, now: datetime
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
        actor: AuthenticatedActor,
        *,
        action: str,
        resource_type: str,
        resource_id: UUID,
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
                resource_type=resource_type,
                resource_id=resource_id,
                request_id=request_id,
                idempotency_key=key,
                before_data=before,
                after_data=after,
                reason_data={},
            )
        )

    async def _finish(
        self, record: IdempotencyRecordModel, resource: PlanningResource, status: int
    ) -> PlanningMutationResult:
        await self._session.flush()
        record.state, record.response_status, record.response_body = (
            IdempotencyState.COMPLETED,
            status,
            _json(resource),
        )
        return PlanningMutationResult(resource=resource, replayed=False)

    async def list_project_weeks(
        self, *, actor: AuthenticatedActor, project_id: UUID, page: int, page_size: int
    ) -> PlanningPage:
        await self._activate(actor)
        predicates = [
            ProjectWeekModel.organization_id == actor.organization_id,
            ProjectWeekModel.project_id == project_id,
            self._visible_project(actor, ProjectWeekModel.project_id),
        ]
        total = (
            await self._session.scalar(
                select(func.count()).select_from(ProjectWeekModel).where(*predicates)
            )
            or 0
        )
        models = await self._session.scalars(
            select(ProjectWeekModel)
            .where(*predicates)
            .order_by(ProjectWeekModel.week_number, ProjectWeekModel.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return PlanningPage(tuple(_project_week(item) for item in models), page, page_size, total)

    async def get_project_week(
        self, *, actor: AuthenticatedActor, project_id: UUID, project_week_id: UUID
    ) -> ProjectWeek | None:
        await self._activate(actor)
        model = await self._session.scalar(
            select(ProjectWeekModel).where(
                ProjectWeekModel.organization_id == actor.organization_id,
                ProjectWeekModel.project_id == project_id,
                ProjectWeekModel.id == project_week_id,
                self._visible_project(actor, ProjectWeekModel.project_id),
            )
        )
        return _project_week(model) if model else None

    async def create_project_week(
        self,
        *,
        actor: AuthenticatedActor,
        draft: ProjectWeekDraft,
        request_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> PlanningMutationResult:
        await self._activate(actor)
        operation = "project_week.create"
        replay = await self._replay(actor, operation, idempotency_key, request_fingerprint)
        if replay is not None:
            assert isinstance(replay, PlanningMutationResult)
            return replay
        await self._lock_project(actor, draft.project_id)
        await self._ensure_week_range_available(
            actor,
            project_id=draft.project_id,
            start_date=draft.start_date,
            end_date=draft.end_date,
        )
        now = datetime.now(UTC)
        record = self._record(actor, operation, idempotency_key, request_fingerprint, now)
        model = ProjectWeekModel(
            id=uuid4(),
            organization_id=actor.organization_id,
            project_id=draft.project_id,
            week_number=draft.week_number,
            start_date=draft.start_date,
            end_date=draft.end_date,
            objective=draft.objective,
            status=draft.status,
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
        resource = _project_week(model)
        self._audit(
            actor,
            action="project_week.created",
            resource_type="project_week",
            resource_id=resource.id,
            request_id=request_id,
            key=idempotency_key,
            before={},
            after={"week_number": resource.week_number, "status": resource.status.value},
        )
        return await self._finish(record, resource, 201)

    async def update_project_week(
        self,
        *,
        actor: AuthenticatedActor,
        project_id: UUID,
        project_week_id: UUID,
        patch: ProjectWeekPatch,
        expected_version: int,
        request_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> PlanningMutationResult:
        await self._activate(actor)
        operation = f"project_week.update:{project_week_id}"
        replay = await self._replay(actor, operation, idempotency_key, request_fingerprint)
        if replay is not None:
            assert isinstance(replay, PlanningMutationResult)
            return replay
        await self._lock_project(actor, project_id)
        model = await self._session.scalar(
            select(ProjectWeekModel)
            .where(
                ProjectWeekModel.organization_id == actor.organization_id,
                ProjectWeekModel.project_id == project_id,
                ProjectWeekModel.id == project_week_id,
            )
            .with_for_update()
        )
        if model is None:
            raise PlanningNotFoundError
        if model.version != expected_version:
            raise PlanningVersionMismatchError(model.version)
        current = _project_week(model)
        updated = current.apply(patch, updated_at=datetime.now(UTC))
        await self._ensure_week_range_available(
            actor,
            project_id=project_id,
            start_date=updated.start_date,
            end_date=updated.end_date,
            exclude_id=project_week_id,
        )
        record = self._record(
            actor, operation, idempotency_key, request_fingerprint, updated.updated_at
        )
        before = _json(current)
        model.week_number = updated.week_number
        model.start_date = updated.start_date
        model.end_date = updated.end_date
        model.objective = updated.objective
        model.status = updated.status
        model.version = updated.version
        model.updated_at = updated.updated_at
        model.updated_by_membership_id = actor.membership_id
        await self._session.flush()
        await capture_project_baselines(
            self._session,
            actor,
            model.project_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
        resource = _project_week(model)
        self._audit(
            actor,
            action="project_week.updated",
            resource_type="project_week",
            resource_id=resource.id,
            request_id=request_id,
            key=idempotency_key,
            before=before,
            after=_json(resource),
        )
        return await self._finish(record, resource, 200)

    async def delete_project_week(
        self,
        *,
        actor: AuthenticatedActor,
        project_id: UUID,
        project_week_id: UUID,
        expected_version: int,
        request_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> PlanningDeleteResult:
        await self._activate(actor)
        operation = f"project_week.delete:{project_week_id}"
        replay = await self._replay(actor, operation, idempotency_key, request_fingerprint)
        if replay is not None:
            assert isinstance(replay, PlanningDeleteResult)
            return replay
        await self._lock_project(actor, project_id)
        model = await self._session.scalar(
            select(ProjectWeekModel)
            .where(
                ProjectWeekModel.organization_id == actor.organization_id,
                ProjectWeekModel.project_id == project_id,
                ProjectWeekModel.id == project_week_id,
            )
            .with_for_update()
        )
        if model is None:
            raise PlanningNotFoundError
        if model.version != expected_version:
            raise PlanningVersionMismatchError(model.version)
        if model.status == ProjectWeekStatus.COMPLETED or await self._session.scalar(
            select(TaskModel.id)
            .where(
                TaskModel.organization_id == actor.organization_id,
                TaskModel.project_week_id == project_week_id,
            )
            .limit(1)
        ):
            raise ProjectWeekDeleteBlockedError
        has_history = await self._session.scalar(
            select(WeeklyPlanBaselineModel.id)
            .where(
                WeeklyPlanBaselineModel.organization_id == actor.organization_id,
                WeeklyPlanBaselineModel.project_week_id == project_week_id,
                func.jsonb_array_length(WeeklyPlanBaselineModel.payload["task_entries"]) > 0,
            )
            .limit(1)
        )
        if has_history is not None:
            raise ProjectWeekDeleteBlockedError
        now = datetime.now(UTC)
        record = self._record(actor, operation, idempotency_key, request_fingerprint, now)
        deleted_version = model.version + 1
        self._audit(
            actor,
            action="project_week.deleted",
            resource_type="project_week",
            resource_id=project_week_id,
            request_id=request_id,
            key=idempotency_key,
            before=_json(_project_week(model)),
            after={},
        )
        await self._session.delete(model)
        await self._session.flush()
        record.state, record.response_status, record.response_body = (
            IdempotencyState.COMPLETED,
            200,
            {"kind": "deleted", "id": str(project_week_id), "version": deleted_version},
        )
        return PlanningDeleteResult(project_week_id, deleted_version, False)

    async def list_goals(
        self, *, actor: AuthenticatedActor, project_id: UUID | None, page: int, page_size: int
    ) -> PlanningPage:
        await self._activate(actor)
        predicates = [
            GoalModel.organization_id == actor.organization_id,
            self._visible_project(actor, GoalModel.project_id),
        ]
        if project_id is not None:
            predicates.append(GoalModel.project_id == project_id)
        total = (
            await self._session.scalar(
                select(func.count()).select_from(GoalModel).where(*predicates)
            )
            or 0
        )
        models = await self._session.scalars(
            select(GoalModel)
            .where(*predicates)
            .order_by(GoalModel.created_at, GoalModel.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return PlanningPage(tuple(_goal(item) for item in models), page, page_size, total)

    async def get_goal(self, *, actor: AuthenticatedActor, goal_id: UUID) -> Goal | None:
        await self._activate(actor)
        model = await self._session.scalar(
            select(GoalModel).where(
                GoalModel.organization_id == actor.organization_id,
                GoalModel.id == goal_id,
                self._visible_project(actor, GoalModel.project_id),
            )
        )
        return _goal(model) if model else None

    async def create_goal(
        self,
        *,
        actor: AuthenticatedActor,
        draft: GoalDraft,
        request_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> PlanningMutationResult:
        await self._activate(actor)
        operation = "goal.create"
        replay = await self._replay(actor, operation, idempotency_key, request_fingerprint)
        if replay is not None:
            assert isinstance(replay, PlanningMutationResult)
            return replay
        await self._project(actor, draft.project_id)
        if await self._session.scalar(
            select(GoalModel.id).where(
                GoalModel.organization_id == actor.organization_id,
                GoalModel.project_id == draft.project_id,
            )
        ):
            raise GoalAlreadyExistsError
        now = datetime.now(UTC)
        record = self._record(actor, operation, idempotency_key, request_fingerprint, now)
        model = GoalModel(
            id=uuid4(),
            organization_id=actor.organization_id,
            project_id=draft.project_id,
            title=draft.title,
            description=draft.description,
            expected_outcomes=list(draft.expected_outcomes),
            target_date=draft.target_date,
            version=1,
            created_by_membership_id=actor.membership_id,
            updated_by_membership_id=actor.membership_id,
            created_at=now,
            updated_at=now,
        )
        self._session.add(model)
        await self._session.flush()
        resource = _goal(model)
        self._audit(
            actor,
            action="goal.created",
            resource_type="goal",
            resource_id=resource.id,
            request_id=request_id,
            key=idempotency_key,
            before={},
            after={"title": resource.title},
        )
        return await self._finish(record, resource, 201)

    async def update_goal(
        self,
        *,
        actor: AuthenticatedActor,
        goal_id: UUID,
        patch: GoalPatch,
        expected_version: int,
        request_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> PlanningMutationResult:
        return await self._update_simple(
            actor,
            GoalModel,
            _goal,
            "goal",
            goal_id,
            patch,
            expected_version,
            request_id,
            idempotency_key,
            request_fingerprint,
            (
                ("title", patch.title_supplied, patch.title),
                ("description", patch.description_supplied, patch.description),
                (
                    "expected_outcomes",
                    patch.expected_outcomes_supplied,
                    list(patch.expected_outcomes),
                ),
                ("target_date", patch.target_date_supplied, patch.target_date),
            ),
        )

    async def delete_goal(self, **kwargs: Any) -> PlanningDeleteResult:
        return await self._delete_simple("goal", GoalModel, **kwargs)

    async def list_milestones(
        self, *, actor: AuthenticatedActor, project_id: UUID | None, page: int, page_size: int
    ) -> PlanningPage:
        await self._activate(actor)
        predicates = [
            MilestoneModel.organization_id == actor.organization_id,
            self._visible_project(actor, MilestoneModel.project_id),
        ]
        if project_id is not None:
            predicates.append(MilestoneModel.project_id == project_id)
        total = (
            await self._session.scalar(
                select(func.count()).select_from(MilestoneModel).where(*predicates)
            )
            or 0
        )
        models = await self._session.scalars(
            select(MilestoneModel)
            .where(*predicates)
            .order_by(MilestoneModel.position, MilestoneModel.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return PlanningPage(tuple(_milestone(item) for item in models), page, page_size, total)

    async def get_milestone(
        self, *, actor: AuthenticatedActor, milestone_id: UUID
    ) -> Milestone | None:
        await self._activate(actor)
        model = await self._session.scalar(
            select(MilestoneModel).where(
                MilestoneModel.organization_id == actor.organization_id,
                MilestoneModel.id == milestone_id,
                self._visible_project(actor, MilestoneModel.project_id),
            )
        )
        return _milestone(model) if model else None

    async def create_milestone(
        self,
        *,
        actor: AuthenticatedActor,
        draft: MilestoneDraft,
        request_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> PlanningMutationResult:
        await self._activate(actor)
        operation = "milestone.create"
        replay = await self._replay(actor, operation, idempotency_key, request_fingerprint)
        if replay is not None:
            assert isinstance(replay, PlanningMutationResult)
            return replay
        await self._project(actor, draft.project_id)
        now = datetime.now(UTC)
        record = self._record(actor, operation, idempotency_key, request_fingerprint, now)
        model = MilestoneModel(
            id=uuid4(),
            organization_id=actor.organization_id,
            project_id=draft.project_id,
            name=draft.name,
            description=draft.description,
            target_date=draft.target_date,
            position=draft.position,
            version=1,
            created_by_membership_id=actor.membership_id,
            updated_by_membership_id=actor.membership_id,
            created_at=now,
            updated_at=now,
        )
        self._session.add(model)
        await self._session.flush()
        resource = _milestone(model)
        self._audit(
            actor,
            action="milestone.created",
            resource_type="milestone",
            resource_id=resource.id,
            request_id=request_id,
            key=idempotency_key,
            before={},
            after={"name": resource.name},
        )
        return await self._finish(record, resource, 201)

    async def update_milestone(
        self,
        *,
        actor: AuthenticatedActor,
        milestone_id: UUID,
        patch: MilestonePatch,
        expected_version: int,
        request_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> PlanningMutationResult:
        if patch.target_date_supplied and patch.target_date is not None:
            invalid = await self._session.scalar(
                select(TaskModel.id)
                .where(
                    TaskModel.organization_id == actor.organization_id,
                    TaskModel.milestone_id == milestone_id,
                    TaskModel.due_date > patch.target_date,
                )
                .limit(1)
            )
            if invalid is not None:
                raise MilestoneDateInvariantError
        return await self._update_simple(
            actor,
            MilestoneModel,
            _milestone,
            "milestone",
            milestone_id,
            patch,
            expected_version,
            request_id,
            idempotency_key,
            request_fingerprint,
            (
                ("name", patch.name_supplied, patch.name),
                ("description", patch.description_supplied, patch.description),
                ("target_date", patch.target_date_supplied, patch.target_date),
                ("position", patch.position_supplied, patch.position),
            ),
        )

    async def delete_milestone(self, **kwargs: Any) -> PlanningDeleteResult:
        return await self._delete_simple("milestone", MilestoneModel, **kwargs)

    async def _edge_projects(
        self, actor: AuthenticatedActor, predecessor: UUID, successor: UUID
    ) -> tuple[UUID, UUID]:
        rows = (
            await self._session.execute(
                select(TaskModel.id, TaskModel.project_id).where(
                    TaskModel.organization_id == actor.organization_id,
                    TaskModel.id.in_((predecessor, successor)),
                )
            )
        ).all()
        mapping = {row.id: row.project_id for row in rows}
        if predecessor not in mapping:
            raise PlanningReferenceError("predecessor_task_id")
        if successor not in mapping:
            raise PlanningReferenceError("successor_task_id")
        return mapping[predecessor], mapping[successor]

    async def _validate_edge(
        self,
        actor: AuthenticatedActor,
        predecessor: UUID,
        successor: UUID,
        exclude_id: UUID | None = None,
    ) -> None:
        left, right = await self._edge_projects(actor, predecessor, successor)
        if left != right:
            raise CrossProjectDependencyError
        await self._lock_project(actor, left)
        duplicate_query = select(TaskDependencyModel.id).where(
            TaskDependencyModel.organization_id == actor.organization_id,
            TaskDependencyModel.predecessor_task_id == predecessor,
            TaskDependencyModel.successor_task_id == successor,
        )
        if exclude_id is not None:
            duplicate_query = duplicate_query.where(TaskDependencyModel.id != exclude_id)
        if await self._session.scalar(duplicate_query):
            raise DuplicateDependencyError
        rows = (
            await self._session.execute(
                select(
                    TaskDependencyModel.id,
                    TaskDependencyModel.predecessor_task_id,
                    TaskDependencyModel.successor_task_id,
                ).where(TaskDependencyModel.organization_id == actor.organization_id)
            )
        ).all()
        adjacency: dict[UUID, set[UUID]] = {}
        for row in rows:
            if exclude_id is not None and row.id == exclude_id:
                continue
            adjacency.setdefault(row.predecessor_task_id, set()).add(row.successor_task_id)
        frontier = [successor]
        seen: set[UUID] = set()
        while frontier:
            current = frontier.pop()
            if current == predecessor:
                raise DependencyCycleError
            if current in seen:
                continue
            seen.add(current)
            frontier.extend(adjacency.get(current, ()))

    async def validate_dependency_edge(
        self, *, actor: AuthenticatedActor, predecessor_task_id: UUID, successor_task_id: UUID
    ) -> None:
        await self._activate(actor)
        await self._validate_edge(actor, predecessor_task_id, successor_task_id)

    async def list_dependencies(
        self,
        *,
        actor: AuthenticatedActor,
        project_id: UUID | None,
        task_id: UUID | None,
        page: int,
        page_size: int,
    ) -> PlanningPage:
        await self._activate(actor)
        query = select(TaskDependencyModel).join(
            TaskModel,
            (TaskModel.organization_id == TaskDependencyModel.organization_id)
            & (TaskModel.id == TaskDependencyModel.predecessor_task_id),
        )
        predicates = [
            TaskDependencyModel.organization_id == actor.organization_id,
            self._visible_project(actor, TaskModel.project_id),
        ]
        if project_id is not None:
            predicates.append(TaskModel.project_id == project_id)
        if task_id is not None:
            predicates.append(
                or_(
                    TaskDependencyModel.predecessor_task_id == task_id,
                    TaskDependencyModel.successor_task_id == task_id,
                )
            )
        total = (
            await self._session.scalar(
                select(func.count()).select_from(query.where(*predicates).subquery())
            )
            or 0
        )
        models = await self._session.scalars(
            query.where(*predicates)
            .order_by(TaskDependencyModel.created_at, TaskDependencyModel.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return PlanningPage(tuple(_dependency(item) for item in models), page, page_size, total)

    async def get_dependency(
        self, *, actor: AuthenticatedActor, dependency_id: UUID
    ) -> TaskDependency | None:
        await self._activate(actor)
        query = (
            select(TaskDependencyModel)
            .join(
                TaskModel,
                (TaskModel.organization_id == TaskDependencyModel.organization_id)
                & (TaskModel.id == TaskDependencyModel.predecessor_task_id),
            )
            .where(
                TaskDependencyModel.organization_id == actor.organization_id,
                TaskDependencyModel.id == dependency_id,
                self._visible_project(actor, TaskModel.project_id),
            )
        )
        model = await self._session.scalar(query)
        return _dependency(model) if model else None

    async def create_dependency(
        self,
        *,
        actor: AuthenticatedActor,
        draft: TaskDependencyDraft,
        request_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> PlanningMutationResult:
        await self._activate(actor)
        operation = "task_dependency.create"
        replay = await self._replay(actor, operation, idempotency_key, request_fingerprint)
        if replay is not None:
            assert isinstance(replay, PlanningMutationResult)
            return replay
        await self._validate_edge(actor, draft.predecessor_task_id, draft.successor_task_id)
        now = datetime.now(UTC)
        record = self._record(actor, operation, idempotency_key, request_fingerprint, now)
        model = TaskDependencyModel(
            id=uuid4(),
            organization_id=actor.organization_id,
            predecessor_task_id=draft.predecessor_task_id,
            successor_task_id=draft.successor_task_id,
            version=1,
            created_by_membership_id=actor.membership_id,
            updated_by_membership_id=actor.membership_id,
            created_at=now,
            updated_at=now,
        )
        self._session.add(model)
        await self._session.flush()
        task = await self._task(actor, model.successor_task_id)
        await capture_project_baselines(
            self._session,
            actor,
            task.project_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
        resource = _dependency(model)
        self._audit(
            actor,
            action="task_dependency.created",
            resource_type="task_dependency",
            resource_id=resource.id,
            request_id=request_id,
            key=idempotency_key,
            before={},
            after={
                "predecessor_task_id": str(resource.predecessor_task_id),
                "successor_task_id": str(resource.successor_task_id),
            },
        )
        return await self._finish(record, resource, 201)

    async def update_dependency(
        self,
        *,
        actor: AuthenticatedActor,
        dependency_id: UUID,
        patch: TaskDependencyPatch,
        expected_version: int,
        request_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> PlanningMutationResult:
        await self._activate(actor)
        model = await self._session.scalar(
            select(TaskDependencyModel).where(
                TaskDependencyModel.organization_id == actor.organization_id,
                TaskDependencyModel.id == dependency_id,
            )
        )
        if model is None:
            raise PlanningNotFoundError
        predecessor = (
            patch.predecessor_task_id if patch.predecessor_supplied else model.predecessor_task_id
        )
        successor = patch.successor_task_id if patch.successor_supplied else model.successor_task_id
        assert predecessor is not None and successor is not None
        old_task = await self._task(actor, model.successor_task_id)
        new_task = await self._task(actor, successor)
        for affected_project_id in sorted({old_task.project_id, new_task.project_id}):
            await self._lock_project(actor, affected_project_id)
        await self._validate_edge(actor, predecessor, successor, dependency_id)
        result = await self._update_simple(
            actor,
            TaskDependencyModel,
            _dependency,
            "task_dependency",
            dependency_id,
            patch,
            expected_version,
            request_id,
            idempotency_key,
            request_fingerprint,
            (
                ("predecessor_task_id", patch.predecessor_supplied, predecessor),
                ("successor_task_id", patch.successor_supplied, successor),
            ),
        )

        if old_task.project_id != new_task.project_id:
            await capture_project_baselines(
                self._session,
                actor,
                old_task.project_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
            )
        return result

    async def delete_dependency(self, **kwargs: Any) -> PlanningDeleteResult:
        return await self._delete_simple("task_dependency", TaskDependencyModel, **kwargs)

    async def list_acceptance_criteria(
        self, *, actor: AuthenticatedActor, task_id: UUID | None, page: int, page_size: int
    ) -> PlanningPage:
        await self._activate(actor)
        query = select(AcceptanceCriterionModel).join(
            TaskModel,
            (TaskModel.organization_id == AcceptanceCriterionModel.organization_id)
            & (TaskModel.id == AcceptanceCriterionModel.task_id),
        )
        predicates = [AcceptanceCriterionModel.organization_id == actor.organization_id]
        if actor.role is MembershipRole.EMPLOYEE:
            predicates.append(TaskModel.assignee_membership_id == actor.membership_id)
        if task_id is not None:
            predicates.append(AcceptanceCriterionModel.task_id == task_id)
        total = (
            await self._session.scalar(
                select(func.count()).select_from(query.where(*predicates).subquery())
            )
            or 0
        )
        models = await self._session.scalars(
            query.where(*predicates)
            .order_by(AcceptanceCriterionModel.position, AcceptanceCriterionModel.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return PlanningPage(tuple(_criterion(item) for item in models), page, page_size, total)

    async def get_acceptance_criterion(
        self, *, actor: AuthenticatedActor, criterion_id: UUID
    ) -> AcceptanceCriterion | None:
        await self._activate(actor)
        query = (
            select(AcceptanceCriterionModel)
            .join(
                TaskModel,
                (TaskModel.organization_id == AcceptanceCriterionModel.organization_id)
                & (TaskModel.id == AcceptanceCriterionModel.task_id),
            )
            .where(
                AcceptanceCriterionModel.organization_id == actor.organization_id,
                AcceptanceCriterionModel.id == criterion_id,
            )
        )
        if actor.role is MembershipRole.EMPLOYEE:
            query = query.where(TaskModel.assignee_membership_id == actor.membership_id)
        model = await self._session.scalar(query)
        return _criterion(model) if model else None

    async def _unique_criterion(
        self,
        actor: AuthenticatedActor,
        task_id: UUID,
        criterion_text: str,
        exclude_id: UUID | None = None,
    ) -> None:
        query = select(AcceptanceCriterionModel.id).where(
            AcceptanceCriterionModel.organization_id == actor.organization_id,
            AcceptanceCriterionModel.task_id == task_id,
            AcceptanceCriterionModel.text == criterion_text,
        )
        if exclude_id is not None:
            query = query.where(AcceptanceCriterionModel.id != exclude_id)
        if await self._session.scalar(query):
            raise DuplicateAcceptanceCriterionError

    async def create_acceptance_criterion(
        self,
        *,
        actor: AuthenticatedActor,
        draft: AcceptanceCriterionDraft,
        request_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> PlanningMutationResult:
        await self._activate(actor)
        operation = "acceptance_criterion.create"
        replay = await self._replay(actor, operation, idempotency_key, request_fingerprint)
        if replay is not None:
            assert isinstance(replay, PlanningMutationResult)
            return replay
        await self._task(actor, draft.task_id)
        await self._unique_criterion(actor, draft.task_id, draft.text)
        now = datetime.now(UTC)
        record = self._record(actor, operation, idempotency_key, request_fingerprint, now)
        model = AcceptanceCriterionModel(
            id=uuid4(),
            organization_id=actor.organization_id,
            task_id=draft.task_id,
            text=draft.text,
            position=draft.position,
            version=1,
            created_by_membership_id=actor.membership_id,
            updated_by_membership_id=actor.membership_id,
            created_at=now,
            updated_at=now,
        )
        self._session.add(model)
        await self._session.flush()
        resource = _criterion(model)
        self._audit(
            actor,
            action="acceptance_criterion.created",
            resource_type="acceptance_criterion",
            resource_id=resource.id,
            request_id=request_id,
            key=idempotency_key,
            before={},
            after={"text": resource.text},
        )
        return await self._finish(record, resource, 201)

    async def update_acceptance_criterion(
        self,
        *,
        actor: AuthenticatedActor,
        criterion_id: UUID,
        patch: AcceptanceCriterionPatch,
        expected_version: int,
        request_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> PlanningMutationResult:
        await self._activate(actor)
        model = await self._session.scalar(
            select(AcceptanceCriterionModel).where(
                AcceptanceCriterionModel.organization_id == actor.organization_id,
                AcceptanceCriterionModel.id == criterion_id,
            )
        )
        if model is None:
            raise PlanningNotFoundError
        if patch.text_supplied and patch.text is not None:
            await self._unique_criterion(actor, model.task_id, patch.text, criterion_id)
        return await self._update_simple(
            actor,
            AcceptanceCriterionModel,
            _criterion,
            "acceptance_criterion",
            criterion_id,
            patch,
            expected_version,
            request_id,
            idempotency_key,
            request_fingerprint,
            (
                ("text", patch.text_supplied, patch.text),
                ("position", patch.position_supplied, patch.position),
            ),
        )

    async def delete_acceptance_criterion(self, **kwargs: Any) -> PlanningDeleteResult:
        return await self._delete_simple("acceptance_criterion", AcceptanceCriterionModel, **kwargs)

    async def _update_simple(
        self,
        actor: AuthenticatedActor,
        model_type: Any,
        convert: Any,
        kind: str,
        resource_id: UUID,
        patch: Any,
        expected_version: int,
        request_id: str,
        idempotency_key: str,
        request_fingerprint: str,
        fields: tuple[tuple[str, bool, object], ...],
    ) -> PlanningMutationResult:
        await self._activate(actor)
        operation = f"{kind}.update:{resource_id}"
        replay = await self._replay(actor, operation, idempotency_key, request_fingerprint)
        if replay is not None:
            assert isinstance(replay, PlanningMutationResult)
            return replay
        model = await self._session.scalar(
            select(model_type)
            .where(
                model_type.organization_id == actor.organization_id, model_type.id == resource_id
            )
            .with_for_update()
        )
        if model is None:
            raise PlanningNotFoundError
        if model.version != expected_version:
            raise PlanningVersionMismatchError(model.version)
        now = datetime.now(UTC)
        record = self._record(actor, operation, idempotency_key, request_fingerprint, now)
        before: dict[str, object] = {}
        after: dict[str, object] = {}
        for field, supplied, value in fields:
            if supplied:
                old = getattr(model, field)
                before[field] = str(old) if isinstance(old, (UUID, date)) else old
                setattr(model, field, value)
                after[field] = str(value) if isinstance(value, (UUID, date)) else value
        model.version += 1
        model.updated_at = now
        model.updated_by_membership_id = actor.membership_id
        await self._session.flush()
        if kind == "task_dependency":
            task = await self._task(actor, model.successor_task_id)
            await capture_project_baselines(
                self._session,
                actor,
                task.project_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
            )
        resource = convert(model)
        self._audit(
            actor,
            action=f"{kind}.updated",
            resource_type=kind,
            resource_id=resource_id,
            request_id=request_id,
            key=idempotency_key,
            before=before,
            after=after,
        )
        return await self._finish(record, resource, 200)

    async def _delete_simple(
        self,
        kind: str,
        model_type: Any,
        *,
        actor: AuthenticatedActor,
        expected_version: int,
        request_id: str,
        idempotency_key: str,
        request_fingerprint: str,
        **ids: Any,
    ) -> PlanningDeleteResult:
        resource_id = next(value for key, value in ids.items() if key.endswith("_id"))
        await self._activate(actor)
        operation = f"{kind}.delete:{resource_id}"
        replay = await self._replay(actor, operation, idempotency_key, request_fingerprint)
        if replay is not None:
            assert isinstance(replay, PlanningDeleteResult)
            return replay
        if kind == "task_dependency":
            edge = await self._session.scalar(
                select(TaskDependencyModel).where(
                    TaskDependencyModel.organization_id == actor.organization_id,
                    TaskDependencyModel.id == resource_id,
                )
            )
            if edge is None:
                raise PlanningNotFoundError
            endpoint = await self._task(actor, edge.successor_task_id)
            await self._lock_project(actor, endpoint.project_id)
        model = await self._session.scalar(
            select(model_type)
            .where(
                model_type.organization_id == actor.organization_id, model_type.id == resource_id
            )
            .with_for_update()
        )
        if model is None:
            raise PlanningNotFoundError
        if model.version != expected_version:
            raise PlanningVersionMismatchError(model.version)
        now = datetime.now(UTC)
        record = self._record(actor, operation, idempotency_key, request_fingerprint, now)
        deleted_version = model.version + 1
        self._audit(
            actor,
            action=f"{kind}.deleted",
            resource_type=kind,
            resource_id=resource_id,
            request_id=request_id,
            key=idempotency_key,
            before={"version": model.version},
            after={},
        )
        task = (
            await self._task(actor, model.successor_task_id) if kind == "task_dependency" else None
        )
        await self._session.delete(model)
        await self._session.flush()
        if task is not None:
            await capture_project_baselines(
                self._session,
                actor,
                task.project_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
            )
        record.state, record.response_status, record.response_body = (
            IdempotencyState.COMPLETED,
            200,
            {"kind": "deleted", "id": str(resource_id), "version": deleted_version},
        )
        return PlanningDeleteResult(resource_id, deleted_version, False)

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
        resource_type = action.split(".", 1)[0]
        self._session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=actor.organization_id,
                actor_membership_id=actor.membership_id,
                action=action,
                outcome=AuditOutcome.REJECTED,
                resource_type=resource_type,
                resource_id=resource_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                before_data={},
                after_data={},
                reason_data={"code": reason_code},
            )
        )


class SqlAlchemyManualPlanningTransactionFactory:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def __call__(self) -> AsyncGenerator[ManualPlanningRepository]:
        async with self._session_factory.begin() as session:
            yield SqlAlchemyManualPlanningRepository(session)
