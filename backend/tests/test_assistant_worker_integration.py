"""PostgreSQL proof for the durable Assistant worker execution boundary."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text

from app.core.config import Settings
from app.core.database import create_database_engine, create_session_factory
from app.modules.assistant.adapters.agent_runtime import (
    AssistantAgentRuntime,
    AssistantTurnExecutor,
    build_agent_registry,
)
from app.modules.assistant.adapters.database_models import (
    AgentCheckpointModel,
    AgentRunModel,
    AssistantEventModel,
    AssistantJobModel,
    AssistantMessageModel,
    AssistantTurnModel,
    OrchestrationRunModel,
)
from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory
from app.modules.assistant.application.execution_service import AssistantExecutionService
from app.modules.assistant.application.job_service import AssistantJobService
from app.modules.assistant.application.service import AssistantService
from app.modules.identity.adapters.auth_repository import SqlAlchemyAuthTransactionFactory
from app.modules.identity.adapters.current_actor import CurrentActorResolver
from app.modules.identity.application.current_actor_service import CurrentActorService
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from work_management_ai.agents.orchestrator.contracts import (
    ExecutionPlan,
    OrchestratorInput,
    OrchestratorOutput,
    OrchestratorStatus,
)
from work_management_ai.runtime.contracts import (
    AgentBudget,
    CapabilityUnavailableResponseBlock,
)
from work_management_ai.runtime.execution_engine import (
    ExecutionCheckpoint,
    ExecutionRecorderPort,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


class _FixedPlanningSnapshot:
    async def get_proposal_version(
        self, *, actor: AuthenticatedActor, proposal_id: UUID
    ) -> int | None:
        return 1


class _TerminalEngine:
    def __init__(self) -> None:
        self.values: list[OrchestratorInput] = []

    async def execute(
        self,
        *,
        orchestration_run_id: UUID,
        value: object,
        recorder: ExecutionRecorderPort,
    ) -> OrchestratorOutput:
        assert isinstance(value, OrchestratorInput)
        self.values.append(value)
        plan = ExecutionPlan(
            objectives=("Answer safely",),
            steps=(),
            unavailable_capabilities=("reporting.generate",),
            response_language="en",
        )
        await recorder.save_checkpoint(
            ExecutionCheckpoint(
                orchestration_run_id=orchestration_run_id,
                sequence=1,
                node="terminal",
                plan=plan,
                completed_step_ids=(),
                agent_result_ids=(),
                remaining_budget=AgentBudget(
                    max_iterations=8,
                    max_tool_calls=0,
                    max_handoffs=6,
                    max_replans=2,
                    timeout_seconds=120,
                ),
            )
        )
        blocks = (
            CapabilityUnavailableResponseBlock(
                capability="reporting.generate",
                message_key="assistant.capability_unavailable",
            ),
        )
        await recorder.append_public_blocks(
            value.conversation_id,  # type: ignore[attr-defined]
            value.turn_id,  # type: ignore[attr-defined]
            blocks,
            f"assistant:{value.turn_id}:terminal",  # type: ignore[attr-defined]
        )
        return OrchestratorOutput(
            execution_plan=plan,
            agent_results=(),
            blocks=blocks,
            completed_step_ids=(),
            status=OrchestratorStatus.COMPLETED,
            stop_reason="CAPABILITY_UNAVAILABLE",
            replans_used=0,
            model_refs=(),
        )


@pytest.mark.asyncio
async def test_worker_commits_transcript_event_and_terminal_state_once() -> None:
    engine = create_database_engine(Settings(environment="test"))
    organization_id, membership_id, user_id = uuid4(), uuid4(), uuid4()
    actor = AuthenticatedActor(
        user_id=user_id,
        email=f"{user_id.hex}@example.test",
        display_name="Assistant worker",
        membership_id=membership_id,
        organization_id=organization_id,
        organization_name="Assistant worker tenant",
        role=MembershipRole.MANAGER,
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO organizations (id, slug, name) VALUES (:id, :slug, 'Worker')"),
                {"id": organization_id, "slug": f"worker-{organization_id.hex}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, email_normalized, email_display, display_name, password_hash) "
                    "VALUES (:id, :email, :email, 'Worker', 'hash')"
                ),
                {"id": user_id, "email": actor.email},
            )
            await connection.execute(
                text(
                    "INSERT INTO memberships (id, organization_id, user_id, role) "
                    "VALUES (:id, :org, :user, 'MANAGER')"
                ),
                {"id": membership_id, "org": organization_id, "user": user_id},
            )

        session_factory = create_session_factory(engine)
        transactions = PostgreSQLAssistantTransactionFactory(session_factory)
        service = AssistantService(
            transaction_factory=transactions,
            planning_snapshot=_FixedPlanningSnapshot(),
            orchestrator_version="1.0.0",
            orchestrator_fingerprint="orchestrator-test",
        )
        conversation = await service.create_conversation(
            actor=actor,
            locale="en",
            title="Worker test",
            request_id="request-create",
            idempotency_key="conversation-create",
        )
        workflow_run_id = uuid4()
        proposal_id = uuid4()
        submitted = await service.post_message(
            actor=actor,
            conversation_id=conversation.conversation.id,
            message="Extend the plan through November",
            locale="en",
            card_action={
                "kind": "PLANNING_REVISE",
                "workflow_run_id": str(workflow_run_id),
                "proposal_id": str(proposal_id),
            },
            if_match_version=1,
            request_id="request-message",
            idempotency_key="message-create",
        )
        registry, _ = build_agent_registry()
        actor_resolver = CurrentActorResolver(
            CurrentActorService(SqlAlchemyAuthTransactionFactory(session_factory))
        )
        terminal_engine = _TerminalEngine()
        executor = AssistantTurnExecutor(
            transaction_factory=transactions,
            registry=registry,
            engine_factory=lambda _: terminal_engine,  # type: ignore[arg-type]
        )
        execution = AssistantExecutionService(
            actor_resolver=actor_resolver,
            runtime=AssistantAgentRuntime(executor),
        )
        jobs = AssistantJobService(
            transaction_factory=transactions,
            handler=execution.execute,
            organization_scopes={organization_id},
        )

        assert await jobs.run_once(worker_id="assistant-worker", organization_id=organization_id)
        assert not await jobs.run_once(
            worker_id="assistant-worker", organization_id=organization_id
        )

        async with transactions(actor) as transaction:
            session = transaction.session
            assert await session.scalar(select(func.count()).select_from(AgentRunModel)) == 1
            assert await session.scalar(select(func.count()).select_from(AgentCheckpointModel)) == 1
            assert (
                await session.scalar(select(func.count()).select_from(AssistantMessageModel)) == 3
            )
            assert await session.scalar(select(func.count()).select_from(AssistantEventModel)) == 3
            job_status = await session.scalar(
                select(AssistantJobModel.status).where(AssistantJobModel.id == submitted.job.id)
            )
            turn_status = await session.scalar(
                select(AssistantTurnModel.status).where(AssistantTurnModel.id == submitted.turn.id)
            )
            run_status = await session.scalar(
                select(OrchestrationRunModel.status).where(
                    OrchestrationRunModel.id == submitted.run.id
                )
            )
            assert (job_status, turn_status, run_status) == (
                "COMPLETED",
                "COMPLETED",
                "COMPLETED",
            )
        assert len(terminal_engine.values) == 1
        active = terminal_engine.values[0].active_context.active_planning
        assert active is not None
        assert active.model_dump(mode="json") == {
            "workflow_run_id": str(workflow_run_id),
            "workflow_status": "WAITING_FOR_DECISION",
            "proposal_id": str(proposal_id),
            "proposal_version": 1,
            "proposal_status": "READY_FOR_DECISION",
            "requested_operation": "REVISE",
            "workflow_version": "2.0.0",
            "draft_id": None,
            "draft_version": None,
            "draft_fingerprint": None,
            "anchor_date": None,
        }
    finally:
        await engine.dispose()
