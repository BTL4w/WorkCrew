"""Permission-safe Risk tools and history/SSE projection via application reads."""

from typing import Any, Protocol, cast
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.risk.application.read_service import RiskReadService
from app.modules.work.application.task_service import TaskService
from work_management_ai.agents.risk.contracts import RiskExplanationInput
from work_management_ai.runtime.contracts import (
    JsonValue,
    ToolExecutionRequest,
    ToolExecutionResult,
)
from work_management_ai.tools.risk.contracts import RiskToolInput


class ActorResolver(Protocol):
    async def resolve(
        self, *, organization_id: UUID, membership_id: UUID
    ) -> AuthenticatedActor | None: ...


class RiskToolAdapter:
    def __init__(self, *, actors: ActorResolver, tasks: TaskService, reads: RiskReadService):
        self.actors, self.tasks, self.reads = actors, tasks, reads

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        try:
            if request.tool_id != "risk.read":
                raise ValueError("TOOL_NOT_ALLOWED")
            actor = await self.actors.resolve(
                organization_id=request.actor.organization_id,
                membership_id=request.actor.membership_id,
            )
            if (
                actor is None
                or actor.organization_id != request.actor.organization_id
                or actor.membership_id != request.actor.membership_id
            ):
                raise ValueError("ACTOR_UNAVAILABLE")
            value = RiskToolInput.model_validate(request.typed_input)
            try:
                task_id = UUID(value.task_reference)
            except ValueError:
                matches = await self.tasks.find_visible_tasks_by_title(
                    actor=actor, query=value.task_reference, limit=2
                )
                if len(matches) != 1:
                    raise ValueError("TASK_AMBIGUOUS_OR_NOT_FOUND") from None
                task_id = matches[0].id
            context = await self.reads.read(actor, task_id, value.expected_fingerprint)
            output = RiskExplanationInput.model_validate(context.model_dump(mode="json"))
            return ToolExecutionResult(
                status="SUCCEEDED",
                typed_output=cast(dict[str, JsonValue], output.model_dump(mode="json")),
            )
        except Exception:
            return ToolExecutionResult(
                status="REJECTED", typed_output={}, safe_error_code="RISK_CONTEXT_UNAVAILABLE"
            )


class RiskBlockProjector:
    def __init__(self, reads: RiskReadService):
        self.reads = reads

    async def project(self, actor: AuthenticatedActor, value: Any) -> Any:
        if isinstance(value, list | tuple):
            return [await self.project(actor, item) for item in cast(list[Any], value)]
        if not isinstance(value, dict):
            return value
        value = cast(dict[str, Any], value)
        if value.get("kind") == "work_evidence":
            try:
                for ref in value.get("evidence", []):
                    if ref.get("resource_type") == "RISK_CONTEXT":
                        await self.reads.read(
                            actor, UUID(ref["resource_id"]), ref["evidence_id"].rsplit(":", 1)[1]
                        )
            except Exception:
                return dict(
                    kind="safe_error",
                    code="RISK_CONTEXT_UNAVAILABLE",
                    message_key="risk.chat.contextUnavailable",
                    manual_fallback="WORK_VIEW",
                )
        if value.get("kind") == "risk":
            try:
                current = await self.reads.read(actor, UUID(value["task_id"]), value["fingerprint"])
                supplied = RiskExplanationInput.model_validate(
                    {key: value["content"][key] for key in RiskExplanationInput.model_fields}
                )
                if supplied != RiskExplanationInput.model_validate(current.model_dump(mode="json")):
                    raise ValueError("RISK_CONTEXT_MISMATCH")
                return value
            except Exception:
                return dict(
                    kind="safe_error",
                    code="RISK_CONTEXT_UNAVAILABLE",
                    message_key="risk.chat.contextUnavailable",
                    manual_fallback="WORK_VIEW",
                )
        return {key: await self.project(actor, item) for key, item in value.items()}
