"""Fixed server-owned generation scope; models supply no authority."""

from typing import cast

from app.modules.reporting.application.generation_service import GenerationService
from app.modules.reporting.domain.commands import StoreNarrativeCommand
from work_management_ai.agents.reporting.contracts import (
    ReportingProposal,
    ReportingRequest,
    ReportingUsageScope,
)
from work_management_ai.runtime.contracts import (
    JsonValue,
    ToolExecutionRequest,
    ToolExecutionResult,
)

from .agent_runtime import CurrentActorResolverPort


class ReportingToolAdapter:
    def __init__(
        self,
        *,
        actors: CurrentActorResolverPort,
        service: GenerationService,
        scope: ReportingUsageScope,
    ):
        self.actors, self.service, self.scope = actors, service, scope

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        try:
            if (
                request.actor.organization_id != self.scope.organization_id
                or request.actor.membership_id != self.scope.membership_id
            ):
                raise ValueError("REPORTING_SCOPE")
            actor = await self.actors.resolve(
                organization_id=self.scope.organization_id, membership_id=self.scope.membership_id
            )
            if actor is None:
                raise ValueError("ACTOR_UNAVAILABLE")
            output: dict[str, JsonValue]
            if request.tool_id == "reporting.read":
                context = await self.service.read_context(
                    actor=actor,
                    request=ReportingRequest.model_validate(request.typed_input),
                    scope=self.scope,
                )
                output = cast(dict[str, JsonValue], context.model_dump(mode="json"))
            elif request.tool_id == "reporting.propose":
                version = await self.service.store_proposal(
                    actor=actor,
                    command=StoreNarrativeCommand(
                        proposal=ReportingProposal.model_validate(request.typed_input),
                        scope=self.scope,
                    ),
                )
                output = {"version_id": str(version.id)}
            else:
                raise ValueError("TOOL_NOT_ALLOWED")
            return ToolExecutionResult(status="SUCCEEDED", typed_output=output)
        except Exception:
            return ToolExecutionResult(
                status="REJECTED", typed_output={}, safe_error_code="REPORTING_UNAVAILABLE"
            )
