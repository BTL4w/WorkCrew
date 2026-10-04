"""Orchestrator preview-only tool reuses Task 13 application authority and human gates."""

from typing import cast

from app.modules.assistant.adapters.assignment_tools import CurrentActorResolverPort
from app.modules.automations.application.schedule_service import ScheduleService
from app.modules.automations.domain.schedules import ChatScheduleCommand
from work_management_ai.runtime.contracts import (
    DailySummaryResponseBlock,
    JsonValue,
    ToolExecutionRequest,
    ToolExecutionResult,
)


class AutomationToolAdapter:
    def __init__(self, *, actors: CurrentActorResolverPort, schedules: ScheduleService):
        self.actors, self.schedules = actors, schedules

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        try:
            if request.tool_id != "automation.preview" or request.tool_version != "1.0.0":
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
                raise ValueError("ACTOR_CONTEXT_UNAVAILABLE")
            command = ChatScheduleCommand.model_validate(request.typed_input)
            project, draft, version = await self.schedules.prepare_from_chat(
                actor, command, request.call_id
            )
            block = DailySummaryResponseBlock(
                project_id=project,
                draft_id=draft.id if draft else None,
                expected_version=version,
                operation=command.operation,
            )
            return ToolExecutionResult(
                status="SUCCEEDED",
                typed_output=cast(dict[str, JsonValue], block.model_dump(mode="json")),
            )
        except Exception as exc:
            return ToolExecutionResult(
                status="REJECTED",
                typed_output={},
                safe_error_code=str(getattr(exc, "code", "SCHEDULE_PREVIEW_UNAVAILABLE")),
            )
