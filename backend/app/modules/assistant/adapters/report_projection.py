"""Recheck durable operational provenance and sources before delivering status cards."""

from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.reporting.adapters.repository import SQLReportRepository
from app.modules.reporting.adapters.source_reader import source_freshness
from app.modules.reporting.domain.snapshots import ReportMetricSnapshot
from work_management_ai.runtime.contracts import ProjectStatusResponseBlock

from .database_models import AgentRunModel, OrchestrationRunModel, ToolInvocationModel


class ReportStatusContexts:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], timezone: str):
        self.sessions, self.timezone = sessions, timezone

    async def original(self, actor: AuthenticatedActor, run_id: UUID) -> dict[str, Any] | None:
        async with self.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true),"
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            repo = SQLReportRepository(session, actor, self.timezone)
            await repo.authenticate()
            run = await session.scalar(
                select(AgentRunModel)
                .join(
                    OrchestrationRunModel,
                    (AgentRunModel.orchestration_run_id == OrchestrationRunModel.id)
                    & (AgentRunModel.organization_id == OrchestrationRunModel.organization_id),
                )
                .where(
                    AgentRunModel.organization_id == actor.organization_id,
                    AgentRunModel.id == run_id,
                    AgentRunModel.agent_id == "reporting",
                    OrchestrationRunModel.actor_membership_id == actor.membership_id,
                )
            )
            if run is None:
                raise ValueError("STATUS_OWNER")
            rows = (
                await session.scalars(
                    select(ToolInvocationModel)
                    .where(
                        ToolInvocationModel.organization_id == actor.organization_id,
                        ToolInvocationModel.agent_run_id == run_id,
                        ToolInvocationModel.tool_id == "reporting.chat",
                        ToolInvocationModel.status == "SUCCEEDED",
                    )
                    .order_by(ToolInvocationModel.created_at)
                )
            ).all()
            output = next(
                (
                    r.typed_output
                    for r in rows
                    if r.typed_input.get("operation") in {"PREPARE_REPORT", "EXPLAIN_STATUS"}
                ),
                None,
            )
            if not output:
                return None
            if "context" not in output:
                card = output.get("card")
                if card is not None:
                    result = await repo.get(
                        UUID(card["report_id"]), version_id=UUID(card["report_version_id"])
                    )
                    for ref in result.snapshot.sources:
                        if await source_freshness(session, actor, ref) == "UNAVAILABLE":
                            raise ValueError("REPORT_SOURCE_UNAVAILABLE")
                return output
            snapshot = ReportMetricSnapshot.model_validate(output["context"]["snapshot"])
            if snapshot.organization_id != actor.organization_id or not snapshot.verified_hash():
                raise ValueError("STATUS_PROVENANCE")
            await repo.authorize_project(snapshot.project_id)
            for ref in snapshot.sources:
                if await source_freshness(session, actor, ref) == "UNAVAILABLE":
                    raise ValueError("STATUS_SOURCE_UNAVAILABLE")
            return output

    async def read(self, actor: AuthenticatedActor, run_id: UUID) -> dict[str, Any]:
        output = await self.original(actor, run_id)
        if not output or "context" not in output:
            raise ValueError("STATUS_CONTEXT")
        return output

    async def project(self, actor: AuthenticatedActor, value: dict[str, Any]) -> dict[str, Any]:
        card = ProjectStatusResponseBlock.model_validate(value)
        output = await self.read(actor, card.context_run_id)
        original = ProjectStatusResponseBlock.model_validate(output["card"])
        if card.model_copy(update={"analysis": (), "analysis_state": "UNAVAILABLE"}) != original:
            raise ValueError("STATUS_CARD_FORGED")
        async with self.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text("SELECT set_config('app.organization_id',:org,true)"),
                {"org": str(actor.organization_id)},
            )
            stored = await session.scalar(
                select(AgentRunModel.typed_output).where(
                    AgentRunModel.organization_id == actor.organization_id,
                    AgentRunModel.id == card.context_run_id,
                )
            )
            if not stored or stored.get("typed_output", {}).get("card") != card.model_dump(
                mode="json"
            ):
                raise ValueError("STATUS_ANALYSIS_FORGED")
        return card.model_dump(mode="json")
