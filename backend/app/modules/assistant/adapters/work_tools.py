"""Read-only adapters from Agent Tools to Work application services.

This module intentionally contains no SQLAlchemy imports.  It re-resolves the
worker actor and then uses the same application services as the manual product.
"""

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from app.modules.assistant.application.ports import AssistantTransactionFactory
from app.modules.assistant.domain.models import InvocationStatus, ToolInvocation
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.work.application.project_service import ProjectService
from app.modules.work.application.task_service import TaskService
from app.modules.work.domain.projects import ProjectNotFoundError
from app.modules.work.domain.tasks import TaskNotFoundError, TaskStatus
from app.modules.work.planning.application.manual_service import (
    ManualPlanningService,
    PlanningNotFoundError,
)
from work_management_ai.agents.work_intelligence.contracts import EvidenceItem
from work_management_ai.runtime.contracts import (
    ActorReference,
    ContextReference,
    JsonValue,
    ToolExecutionRequest,
    ToolExecutionResult,
    ToolExecutorPort,
)
from work_management_ai.runtime.tool_registry import ToolRegistry, ToolRegistryError
from work_management_ai.tools.work.read_my_tasks.contracts import ReadMyTasksInput
from work_management_ai.tools.work.read_resource.contracts import ReadResourceInput


def _fingerprint(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(payload.encode()).hexdigest()


class CurrentActorResolverPort(Protocol):
    async def resolve(
        self, *, organization_id: UUID, membership_id: UUID
    ) -> AuthenticatedActor | None: ...


class RecordingToolExecutor:
    """Persist a stable Tool-call identity and replay its terminal safe result."""

    def __init__(
        self,
        *,
        transaction_factory: AssistantTransactionFactory,
        tool_registry: ToolRegistry,
        backend: ToolExecutorPort,
    ) -> None:
        self._transactions = transaction_factory
        self._registry = tool_registry
        self._backend = backend

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        major = request.tool_version.split(".", maxsplit=1)[0]
        try:
            registered = self._registry.resolve(f"{request.tool_id}@{major}")
        except ToolRegistryError:
            return ToolExecutionResult(
                status="REJECTED", typed_output={}, safe_error_code="TOOL_NOT_ALLOWED"
            )
        async with self._transactions(request.actor.organization_id) as transaction:
            invocation = await transaction.repository.get_tool_invocation(
                organization_id=request.actor.organization_id,
                agent_run_id=request.agent_run_id,
                dedupe_key=request.call_id,
            )
            if invocation is None:
                invocation = ToolInvocation(
                    id=uuid5(
                        NAMESPACE_URL,
                        f"tool:{request.agent_run_id}:{request.call_id}",
                    ),
                    organization_id=request.actor.organization_id,
                    agent_run_id=request.agent_run_id,
                    tool_id=request.tool_id,
                    tool_version=request.tool_version,
                    risk_level=registered.manifest.risk_level.value,
                    typed_input=cast(dict[str, object], request.typed_input),
                    typed_output=None,
                    context_references=(),
                    status=InvocationStatus.RUNNING,
                    idempotency_key=request.idempotency_key,
                    dedupe_key=request.call_id,
                    safe_error_code=None,
                )
                await transaction.repository.append_tool_invocation(invocation=invocation)
            elif not self._same_request(invocation, request):
                await transaction.commit()
                return ToolExecutionResult(
                    status="REJECTED",
                    typed_output={},
                    safe_error_code="TOOL_INVOCATION_IDENTITY_CONFLICT",
                )
            elif invocation.status.is_terminal:
                await transaction.commit()
                if request.tool_id in {
                    "risk.read",
                    "automation.preview",
                    "reporting.read",
                    "reporting.chat",
                }:
                    # Read replay is operationally idempotent, never cached authority.
                    return await self._backend.execute(request)
                return ToolExecutionResult(
                    status=cast(str, invocation.status.value),  # type: ignore[arg-type]
                    typed_output=cast(dict[str, JsonValue], invocation.typed_output or {}),
                    evidence=tuple(
                        ContextReference.model_validate(value)
                        for value in invocation.context_references
                    ),
                    safe_error_code=invocation.safe_error_code,
                )
            await transaction.commit()

        try:
            result = await self._backend.execute(request)
        except Exception:
            result = ToolExecutionResult(
                status="FAILED",
                typed_output={},
                safe_error_code="TOOL_EXECUTION_FAILED",
            )
        async with self._transactions(request.actor.organization_id) as transaction:
            await transaction.repository.finish_tool_invocation(
                organization_id=request.actor.organization_id,
                invocation_id=invocation.id,
                status=result.status,
                typed_output=cast(dict[str, object], result.typed_output),
                context_references=tuple(
                    cast(dict[str, object], item.model_dump(mode="json"))
                    for item in result.evidence
                ),
                safe_error_code=result.safe_error_code,
            )
            await transaction.commit()
        return result

    @staticmethod
    def _same_request(invocation: ToolInvocation, request: ToolExecutionRequest) -> bool:
        return (
            invocation.tool_id == request.tool_id
            and invocation.tool_version == request.tool_version
            and invocation.typed_input == request.typed_input
            and invocation.idempotency_key == request.idempotency_key
        )


def _task_evidence(task: object) -> EvidenceItem:
    # Task is intentionally structural here: application services own its model.
    return EvidenceItem(
        evidence_id=f"TASK:{task.id}:{task.version}",  # type: ignore[attr-defined]
        resource_type="TASK",
        resource_id=task.id,  # type: ignore[attr-defined]
        resource_version=task.version,  # type: ignore[attr-defined]
        fields={
            "title": task.title,  # type: ignore[attr-defined]
            "project_id": str(task.project_id),  # type: ignore[attr-defined]
            "status": task.status.value,  # type: ignore[attr-defined]
            "due_date": task.due_date.isoformat() if task.due_date else None,  # type: ignore[attr-defined]
        },
        observed_at=datetime.now(UTC),
    )


class WorkToolExecutor:
    def __init__(
        self,
        *,
        actor_resolver: CurrentActorResolverPort,
        tool_registry: ToolRegistry,
        task_service: TaskService,
        project_service: ProjectService,
        planning_service: ManualPlanningService,
    ) -> None:
        self._actor_resolver = actor_resolver
        self._registry = tool_registry
        self._tasks = task_service
        self._projects = project_service
        self._planning = planning_service

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        major = request.tool_version.split(".", maxsplit=1)[0]
        reference = f"{request.tool_id}@{major}"
        try:
            registered = self._registry.resolve(reference)
        except ToolRegistryError:
            return self._rejected("TOOL_NOT_ALLOWED")
        actor = await self._resolve(request.actor)
        if actor is None:
            return self._rejected("ACTOR_CONTEXT_UNAVAILABLE")
        if actor.role.value not in registered.manifest.roles:
            return self._rejected("TOOL_NOT_ALLOWED")
        if request.tool_id == "work.read_my_tasks":
            return await self._read_my_tasks(actor, request)
        if request.tool_id == "work.read_resource":
            return await self._read_resource(actor, request)
        return self._rejected("TOOL_NOT_ALLOWED")

    async def _resolve(self, reference: ActorReference) -> AuthenticatedActor | None:
        actor = await self._actor_resolver.resolve(
            organization_id=reference.organization_id, membership_id=reference.membership_id
        )
        if actor is None:
            return None
        if (
            actor.organization_id != reference.organization_id
            or actor.membership_id != reference.membership_id
        ):
            return None
        return actor

    async def _read_my_tasks(
        self, actor: AuthenticatedActor, request: ToolExecutionRequest
    ) -> ToolExecutionResult:
        try:
            value = ReadMyTasksInput.model_validate(request.typed_input)
        except ValueError:
            return self._rejected("TOOL_INPUT_INVALID")
        status = TaskStatus(value.status) if value.status else None
        page = await self._tasks.my_tasks(
            actor=actor,
            status=status,
            due_from=value.due_from,
            due_to=value.due_to,
            page=1,
            page_size=value.limit,
        )
        evidence = tuple(_task_evidence(task) for task in page.items)
        next_task = await self._tasks.get_next_task(actor=actor)
        return self._success(
            organization_id=actor.organization_id,
            resolution="UNIQUE" if evidence else "NOT_FOUND",
            evidence=evidence,
            next_task_id=str(next_task.id) if next_task else None,
        )

    async def _read_resource(
        self, actor: AuthenticatedActor, request: ToolExecutionRequest
    ) -> ToolExecutionResult:
        try:
            value = ReadResourceInput.model_validate(request.typed_input).root
        except ValueError:
            return self._rejected("TOOL_INPUT_INVALID")
        if value.resource_type == "TASK":
            try:
                task_id = UUID(value.reference)
            except ValueError:
                matches = await self._tasks.find_visible_tasks_by_title(
                    actor=actor, query=value.reference, limit=20
                )
                if len(matches) != 1:
                    return self._success(
                        organization_id=actor.organization_id,
                        resolution="AMBIGUOUS" if matches else "NOT_FOUND",
                        evidence=(),
                        next_task_id=None,
                    )
                return self._success(
                    organization_id=actor.organization_id,
                    resolution="UNIQUE",
                    evidence=(_task_evidence(matches[0]),),
                    next_task_id=None,
                )
            try:
                task = await self._tasks.get_task(actor=actor, task_id=task_id)
            except TaskNotFoundError:
                return self._success(
                    organization_id=actor.organization_id,
                    resolution="NOT_FOUND",
                    evidence=(),
                    next_task_id=None,
                )
            return self._success(
                organization_id=actor.organization_id,
                resolution="UNIQUE",
                evidence=(_task_evidence(task),),
                next_task_id=None,
            )
        if value.resource_type == "PROJECT":
            try:
                project = await self._projects.get_project(
                    actor=actor, project_id=UUID(value.reference)
                )
            except (ValueError, ProjectNotFoundError):
                return self._success(
                    organization_id=actor.organization_id,
                    resolution="NOT_FOUND",
                    evidence=(),
                    next_task_id=None,
                )
            observed_at = datetime.now(UTC)
            evidence = EvidenceItem(
                evidence_id=f"PROJECT:{project.id}:{project.version}",
                resource_type="PROJECT",
                resource_id=project.id,
                resource_version=project.version,
                fields={"name": project.name, "description": project.description},
                observed_at=observed_at,
            )
            return self._success(
                organization_id=actor.organization_id,
                resolution="UNIQUE",
                evidence=(evidence,),
                next_task_id=None,
            )
        if value.resource_type == "DEPENDENCY":
            try:
                resource = await self._planning.get_dependency(
                    actor=actor, dependency_id=UUID(value.reference)
                )
            except (ValueError, PlanningNotFoundError):
                return self._not_found(actor.organization_id)
            evidence = EvidenceItem(
                evidence_id=f"DEPENDENCY:{resource.id}:{resource.version}",
                resource_type="DEPENDENCY",
                resource_id=resource.id,
                resource_version=resource.version,
                fields={
                    "predecessor_task_id": str(resource.predecessor_task_id),
                    "successor_task_id": str(resource.successor_task_id),
                },
                observed_at=datetime.now(UTC),
            )
            return self._success(
                organization_id=actor.organization_id,
                resolution="UNIQUE",
                evidence=(evidence,),
                next_task_id=None,
            )
        if value.resource_type == "ACCEPTANCE_CRITERION":
            try:
                resource = await self._planning.get_acceptance_criterion(
                    actor=actor, criterion_id=UUID(value.reference)
                )
            except (ValueError, PlanningNotFoundError):
                return self._not_found(actor.organization_id)
            evidence = EvidenceItem(
                evidence_id=f"ACCEPTANCE_CRITERION:{resource.id}:{resource.version}",
                resource_type="ACCEPTANCE_CRITERION",
                resource_id=resource.id,
                resource_version=resource.version,
                fields={
                    "task_id": str(resource.task_id),
                    "text": resource.text,
                    "position": resource.position,
                },
                observed_at=datetime.now(UTC),
            )
            return self._success(
                organization_id=actor.organization_id,
                resolution="UNIQUE",
                evidence=(evidence,),
                next_task_id=None,
            )
        return self._not_found(actor.organization_id)

    @classmethod
    def _not_found(cls, organization_id: UUID) -> ToolExecutionResult:
        return cls._success(
            organization_id=organization_id,
            resolution="NOT_FOUND",
            evidence=(),
            next_task_id=None,
        )

    @staticmethod
    def _rejected(code: str) -> ToolExecutionResult:
        return ToolExecutionResult(status="REJECTED", typed_output={}, safe_error_code=code)

    @staticmethod
    def _success(
        *,
        organization_id: UUID,
        resolution: str,
        evidence: tuple[EvidenceItem, ...],
        next_task_id: str | None,
    ) -> ToolExecutionResult:
        refs = tuple(
            ContextReference(
                reference_id=item.resource_id,
                organization_id=organization_id,
                resource_type=item.resource_type,
                resource_id=item.resource_id,
                version=item.resource_version,
                fingerprint=_fingerprint(item.fields),
                observed_at=item.observed_at,
            )
            for item in evidence
        )
        return ToolExecutionResult(
            status="SUCCEEDED",
            typed_output={
                "resolution": resolution,
                "evidence": [item.model_dump(mode="json") for item in evidence],
                "next_task_id": next_task_id,
            },
            evidence=refs,
        )
