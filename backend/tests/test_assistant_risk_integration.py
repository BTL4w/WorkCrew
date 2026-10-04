"""Hub-only dispatch and permission checks on persisted card history and SSE."""

import json
import os
from typing import cast
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.assistant.adapters.agent_runtime import build_agent_registry
from app.modules.assistant.adapters.risk_tools import RiskBlockProjector
from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory
from app.modules.assistant.application.event_service import AssistantEventService
from app.modules.assistant.application.service import AssistantService, PlanningSnapshotPort
from app.modules.identity.domain.auth import AuthenticatedActor
from tests.test_daily_update_api_integration import seed_task
from tests.test_evidence_api_integration import Harness, harness
from tests.test_phase4_context_permissions import reader
from tests.test_risk_api_integration import setup
from work_management_ai.agents.orchestrator.contracts import (
    ActiveConversationContext,
    OrchestratorInput,
)
from work_management_ai.agents.orchestrator.harness import OrchestratorHarness
from work_management_ai.agents.risk.harness import RiskHarness
from work_management_ai.agents.risk.tests.test_harness import Actors, Tools, handoff
from work_management_ai.model_gateway.mock import MockModelGateway
from work_management_ai.runtime.contracts import ActorReference, AgentHandoff, AgentId, AgentResult
from work_management_ai.runtime.policy_guard import PolicyGuard

__all__ = ["harness"]


def test_risk_registry_limits_inactive_agents_and_peer_handoffs():
    registry, tools = build_agent_registry()
    with pytest.raises(ValueError, match="AGENT_PHASE_INACTIVE"):
        registry.resolve(AgentId.RISK, "1.0.0", 3)
    manifest = registry.resolve(AgentId.RISK, "1.0.0", 4).manifest
    assert manifest.runtime.max_iterations == 4
    assert manifest.runtime.max_tool_calls == 4
    assert manifest.runtime.max_model_attempts == 1
    assert manifest.runtime.timeout_seconds == 90
    assert manifest.allowed_tools == ("risk.read@1",)
    assert tools.resolve("risk.read@1").manifest.risk_level.value == "READ_ONLY"


@pytest.mark.asyncio
async def test_orchestrator_renders_verified_risk_card_without_resynthesis_or_plan_write():
    tools = Tools()
    specialist = RiskHarness(
        model_gateway=MockModelGateway(fixtures={}), tool_executor=tools, actor_resolver=Actors()
    )
    calls: list[AgentHandoff] = []

    class Specialists:
        async def run_specialist(self, handoff: AgentHandoff) -> AgentResult:
            calls.append(handoff)
            return await specialist.run(handoff)

    registry, _ = build_agent_registry()
    fixture = {
        "objectives": ["Explain risk"],
        "steps": [
            dict(
                step_id="risk",
                target_agent_id="risk",
                target_agent_version="1.0.0",
                capability="risk.explain",
                objective="Explain risk",
                typed_input={"task_reference": str(tools.task)},
                depends_on=[],
                mode="READ_ONLY",
            )
        ],
        "unavailable_capabilities": [],
        "response_language": "vi",
    }
    result = await OrchestratorHarness(
        model_gateway=MockModelGateway(fixtures={"orchestrator.vi.plan": fixture}),
        registry=registry,
        policy_guard=PolicyGuard(),
        actor_resolver=Actors(),
        specialists=Specialists(),
    ).run_turn(
        OrchestratorInput(
            conversation_id=uuid4(),
            turn_id=uuid4(),
            message="Giải thích rủi ro",
            locale="vi",
            actor=handoff(tools).actor,
            active_context=ActiveConversationContext(recent_messages=()),
        )
    )
    assert result.status.value == "COMPLETED"
    assert result.blocks[0].kind == "risk"
    assert result.blocks[0].model_dump()["content"]["score"] == "92"
    assert len(calls) == 1 and calls[0].target_agent_id is AgentId.RISK


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL")
@pytest.mark.asyncio
async def test_get_history_and_sse_remove_card_after_manager_permission_is_revoked(
    harness: Harness,
):
    task = UUID(await seed_task(harness))
    app, risks, _, manager = await setup(harness)
    await risks.request_refresh(manager, task, str(uuid4()))
    assert await risks.run_once(manager)
    reads = reader(harness)
    context = await reads.read(manager, task)
    card = dict(
        kind="risk",
        task_id=str(task),
        fingerprint=context.fingerprint,
        content={
            **context.model_dump(mode="json"),
            "explanation": {
                "observation_explanations": [],
                "limitations": [],
                "recommendations": [],
                "replan_requested": False,
            },
            "fallback": True,
        },
    )
    sessions = async_sessionmaker(
        bind=harness.connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    transactions = PostgreSQLAssistantTransactionFactory(sessions)
    projector = RiskBlockProjector(reads)
    service = AssistantService(
        transaction_factory=transactions,
        planning_snapshot=cast(PlanningSnapshotPort, object()),
        block_projector=projector,
        orchestrator_version="1.0.0",
        orchestrator_fingerprint="a" * 64,
    )
    events = AssistantEventService(transaction_factory=transactions, block_projector=projector)
    app.state.assistant_service = service
    created = await service.create_conversation(
        actor=manager,
        locale="vi",
        title=None,
        request_id=str(uuid4()),
        idempotency_key=str(uuid4()),
    )
    conversation = created.conversation
    await harness.sql(
        "INSERT INTO assistant_messages "
        "(id,organization_id,conversation_id,sequence,role,content_blocks,created_at) "
        "VALUES (:id,:org,:conv,1,'ASSISTANT',CAST(:blocks AS jsonb),now())",
        {
            "id": uuid4(),
            "org": manager.organization_id,
            "conv": conversation.id,
            "blocks": json.dumps([card]),
        },
    )
    await harness.sql(
        "INSERT INTO assistant_events "
        "(id,organization_id,conversation_id,sequence,event_type,public_payload,occurred_at) "
        "VALUES (:id,:org,:conv,2,'test.risk',CAST(:payload AS jsonb),now())",
        {
            "id": uuid4(),
            "org": manager.organization_id,
            "conv": conversation.id,
            "payload": json.dumps({"blocks": [card]}),
        },
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        before = await client.get(f"/api/v1/ai/conversations/{conversation.id}")
        assert before.status_code == 200, before.text
        assert before.json()["messages"][0]["content_blocks"][0]["kind"] == "risk"
        assert "83" in "\n".join(
            await events.replay(actor=manager, conversation_id=conversation.id, after_sequence=1)
        )
        await harness.sql(
            "UPDATE memberships SET role='EMPLOYEE' WHERE id=:id", {"id": manager.membership_id}
        )
        after = await client.get(f"/api/v1/ai/conversations/{conversation.id}")
        assert after.status_code == 200, after.text
        assert after.json()["messages"][0]["content_blocks"][0]["kind"] == "safe_error"
        replay = "\n".join(
            await events.replay(actor=manager, conversation_id=conversation.id, after_sequence=1)
        )
        assert "RISK_CONTEXT_UNAVAILABLE" in replay
        assert "83" not in replay and "Review deadline" not in replay


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL")
@pytest.mark.asyncio
async def test_generated_advice_and_limitations_preserve_snapshot_and_public_card(harness: Harness):
    from app.modules.assistant.adapters.agent_runtime import CurrentAgentActorResolver
    from app.modules.assistant.adapters.risk_tools import RiskToolAdapter
    from app.modules.work.application.task_service import TaskService

    task = UUID(await seed_task(harness))
    _, risks, _, manager = await setup(harness)
    await risks.request_refresh(manager, task, str(uuid4()))
    assert await risks.run_once(manager)
    reads = reader(harness)
    context = await reads.read(manager, task)

    class CurrentActors:
        async def resolve(
            self, *, organization_id: UUID, membership_id: UUID
        ) -> AuthenticatedActor | None:
            assert (
                organization_id == manager.organization_id
                and membership_id == manager.membership_id
            )
            return manager

    tools = RiskToolAdapter(actors=CurrentActors(), tasks=cast(TaskService, object()), reads=reads)
    source = context.observations[0].source_ids[0]
    output = dict(
        observation_explanations=[
            dict(
                text="Review the recorded deadline",
                observation_ids=[context.observations[0].id],
                source_ids=[source],
                assertions=[
                    dict(
                        source_id=source,
                        field="title",
                        value=next(
                            s.values["title"] for s in context.permitted_sources if s.id == source
                        ),
                    )
                ],
            )
        ],
        limitations=["An additional advisory limitation"],
        recommendations=["A new advisory action"],
        replan_requested=False,
    )
    value = handoff(Tools()).model_copy(
        update={
            "actor": manager_ref(manager),
            "typed_input": {
                "task_reference": str(task),
                "locale": "vi",
                "question": "Explain risk",
            },
        }
    )
    result = await RiskHarness(
        model_gateway=MockModelGateway(fixtures={"risk.vi.explain": output}),
        tool_executor=tools,
        actor_resolver=CurrentAgentActorResolver(CurrentActors()),
    ).run(value)
    card = dict(
        kind="risk", task_id=str(task), fingerprint=context.fingerprint, content=result.typed_output
    )
    projected = await RiskBlockProjector(reads).project(manager, card)
    assert projected["kind"] == "risk"
    assert projected["content"]["score"] == "83"
    assert projected["content"]["recommendations"] == list(context.recommendations)
    assert projected["content"]["explanation"]["recommendations"] == ["A new advisory action"]
    assert projected["content"]["explanation"]["limitations"] == [
        "An additional advisory limitation"
    ]


def manager_ref(actor: "AuthenticatedActor") -> "ActorReference":
    from work_management_ai.runtime.contracts import ActorReference

    return ActorReference(organization_id=actor.organization_id, membership_id=actor.membership_id)
