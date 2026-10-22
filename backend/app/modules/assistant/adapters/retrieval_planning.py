"""Canonical read-only provenance for queued Planning, never Manager instructions."""

from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.assistant.adapters.database_models import (
    AgentHandoffModel,
    AgentRunModel,
    OrchestrationRunModel,
)
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.retrieval.application.budget_service import RetrievalBudgetService
from work_management_ai.agents.retrieve.contracts import SourceBinding


class PlanningRetrievalContext:
    def __init__(
        self, *, sessions: async_sessionmaker[AsyncSession], budgets: RetrievalBudgetService
    ):
        self.sessions, self.budgets = sessions, budgets

    async def resolve(
        self, actor: AuthenticatedActor, refs: tuple[SourceBinding, ...]
    ) -> dict[str, Any]:
        if actor.role not in {MembershipRole.MANAGER, MembershipRole.ADMIN}:
            raise ValueError("PLANNING_ROLE_FORBIDDEN")
        if not refs:
            return {}
        async with self.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true),"
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            active = await session.scalar(
                text(
                    "SELECT public.lock_active_membership(:org,:member) "
                    "AND EXISTS(SELECT 1 FROM memberships WHERE organization_id=:org "
                    "AND id=:member AND user_id=:user AND role IN ('MANAGER','ADMIN'))"
                ),
                {
                    "org": actor.organization_id,
                    "member": actor.membership_id,
                    "user": actor.user_id,
                },
            )
            if active is not True:
                raise ValueError("PLANNING_ROLE_FORBIDDEN")
        sources = await self.budgets.load_evidence(
            actor,
            tuple(ref.evidence_id for ref in refs),
            expected_hashes=tuple(ref.fingerprint for ref in refs),
        )
        if sum(len(source.source.text.encode()) for source in sources) > 4000:
            raise ValueError("PLANNING_EVIDENCE_BUDGET_EXCEEDED")
        return {
            "untrusted_web_evidence": [
                dict(
                    evidence_id=str(source.id),
                    fingerprint=source.source.fingerprint,
                    canonical_url=source.source.url.value,
                    method=source.source.method,
                    excerpt=source.source.text,
                    observed_at=source.source.observed_at.isoformat(),
                    expires_at=source.expires_at.isoformat(),
                )
                for source in sources
            ]
        }

    async def load_bound(
        self, actor: AuthenticatedActor, agent_run_id: UUID, refs: tuple[SourceBinding, ...]
    ) -> dict[str, Any]:
        async with self.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true),"
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            run = await session.scalar(
                select(AgentRunModel)
                .join(
                    OrchestrationRunModel,
                    (OrchestrationRunModel.organization_id == AgentRunModel.organization_id)
                    & (OrchestrationRunModel.id == AgentRunModel.orchestration_run_id),
                )
                .where(
                    OrchestrationRunModel.actor_membership_id == actor.membership_id,
                    AgentRunModel.organization_id == actor.organization_id,
                    AgentRunModel.id == agent_run_id,
                    AgentRunModel.agent_id == "planning",
                )
            )
            if run is None or run.typed_input.get("retrieval_context_refs", []) != [
                ref.model_dump(mode="json") for ref in refs
            ]:
                raise ValueError("PLANNING_EVIDENCE_BINDING_INVALID")
            handoff = await session.scalar(
                select(AgentHandoffModel).where(
                    AgentHandoffModel.organization_id == actor.organization_id,
                    AgentHandoffModel.id == run.inbound_handoff_id,
                )
            )
            if handoff is None or tuple(
                (
                    ref.get("resource_id"),
                    ref.get("fingerprint"),
                    ref.get("version"),
                    ref.get("organization_id"),
                )
                for ref in handoff.context_references
                if ref.get("resource_type") == "WEB_SOURCE_SNAPSHOT"
            ) != tuple(
                (str(ref.evidence_id), ref.fingerprint, 1, str(actor.organization_id))
                for ref in refs
            ):
                raise ValueError("PLANNING_EVIDENCE_BINDING_INVALID")
        return await self.resolve(actor, refs)

    async def load(self, actor: AuthenticatedActor, workflow_run_id: UUID) -> dict[str, Any]:
        async with self.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true),"
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            runs = (
                await session.scalars(
                    select(AgentRunModel)
                    .join(
                        OrchestrationRunModel,
                        (OrchestrationRunModel.organization_id == AgentRunModel.organization_id)
                        & (OrchestrationRunModel.id == AgentRunModel.orchestration_run_id),
                    )
                    .where(
                        OrchestrationRunModel.actor_membership_id == actor.membership_id,
                        AgentRunModel.organization_id == actor.organization_id,
                        AgentRunModel.workflow_run_id == workflow_run_id,
                        AgentRunModel.agent_id == "planning",
                    )
                )
            ).all()
            refs = tuple(
                SourceBinding.model_validate(ref)
                for run in runs
                for ref in run.typed_input.get("retrieval_context_refs", [])
            )
        # Empty legacy flows remain independent of retrieval enablement.
        for run in runs:
            bound_refs = tuple(
                SourceBinding.model_validate(ref)
                for ref in run.typed_input.get("retrieval_context_refs", [])
            )
            if bound_refs:
                await self.load_bound(actor, run.id, bound_refs)
        return await self.resolve(actor, refs) if refs else {}
