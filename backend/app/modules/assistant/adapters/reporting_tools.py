"""Fixed server-owned generation scope; models supply no authority."""

from typing import Any, cast

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.reporting.application.chat_service import ReportChatService as ReportChatService
from app.modules.reporting.application.generation_service import GenerationService
from app.modules.reporting.application.report_service import ReportService
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
from .report_projection import ReportStatusContexts
from .risk_tools import RiskBlockProjector


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


class ChatReportingToolAdapter:
    def __init__(
        self,
        *,
        actors: CurrentActorResolverPort,
        service: ReportChatService,
        contexts: ReportStatusContexts,
    ):
        self.actors, self.service, self.contexts = actors, service, contexts

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        try:
            if request.tool_id != "reporting.chat":
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
            if request.typed_input.get("operation") == "REAUTHORIZE":
                if (
                    set(request.typed_input) != {"operation", "context_run_id"}
                    or str(request.agent_run_id) != request.typed_input["context_run_id"]
                ):
                    raise ValueError("CONTEXT_SCOPE")
                output = await self.contexts.read(actor, request.agent_run_id)
            else:
                original = await self.contexts.original(actor, request.agent_run_id)
                output = (
                    original
                    if original is not None
                    else await self.service.prepare(
                        actor=actor,
                        intent=request.typed_input,
                        key=request.idempotency_key,
                        context_run_id=request.agent_run_id,
                    )
                )
            return ToolExecutionResult(
                status="SUCCEEDED", typed_output=cast(dict[str, JsonValue], output)
            )
        except Exception:
            return ToolExecutionResult(
                status="REJECTED", typed_output={}, safe_error_code="REPORT_CONTEXT_UNAVAILABLE"
            )


class ReportBlockProjector:
    def __init__(
        self,
        *,
        reports: ReportService,
        contexts: ReportStatusContexts,
        previous: RiskBlockProjector | None = None,
    ):
        self.reports, self.contexts, self.previous = reports, contexts, previous

    async def project(self, actor: AuthenticatedActor, value: Any) -> Any:
        from work_management_ai.runtime.contracts import ReportResponseBlock

        if isinstance(value, list | tuple):
            return [await self.project(actor, item) for item in cast(list[Any], value)]
        if not isinstance(value, dict):
            return value
        value = cast(dict[str, Any], value)
        raw_context = value.get("response_context")
        context = cast(dict[str, Any], raw_context) if isinstance(raw_context, dict) else {}
        if value.get("kind") == "question" and context.get("reporting") is True:
            try:
                from uuid import UUID

                async with self.reports.transactions(actor) as repo:
                    await repo.authenticate()
                    candidates = context.get("candidates", [])
                    if not isinstance(candidates, list) or len(cast(list[Any], candidates)) > 20:
                        raise ValueError("REPORT_CANDIDATES")
                    for candidate in cast(list[dict[str, Any]], candidates):
                        await repo.authorize_project(UUID(candidate["id"]))
                return value
            except Exception:
                return dict(
                    kind="safe_error",
                    code="REPORT_CONTEXT_UNAVAILABLE",
                    message_key="assistant.report.unavailable",
                    manual_fallback="WORK_VIEW",
                )
        if value.get("kind") in {"report", "project_status"}:
            try:
                if value["kind"] == "project_status":
                    return await self.contexts.project(actor, value)
                card = ReportResponseBlock.model_validate(value)
                original = await self.contexts.original(actor, card.context_run_id)
                if not original or original.get("card") != card.model_dump(mode="json"):
                    raise ValueError("REPORT_CARD_FORGED")
                result = await self.reports.get(
                    actor=actor, report_id=card.report_id, version_id=card.report_version_id
                )
                if (
                    card.project_id != result.report.project_id
                    or card.snapshot_id != result.snapshot.id
                    or card.snapshot_hash != result.snapshot.snapshot_hash
                    or result.narrative_access_state == "UNAVAILABLE"
                ):
                    raise ValueError("REPORT_CARD_SCOPE")
                return card.model_dump(mode="json")
            except Exception:
                return dict(
                    kind="safe_error",
                    code="REPORT_CONTEXT_UNAVAILABLE",
                    message_key="assistant.report.unavailable",
                    manual_fallback="WORK_VIEW",
                )
        projected = {key: await self.project(actor, item) for key, item in value.items()}
        return await self.previous.project(actor, projected) if self.previous else projected
