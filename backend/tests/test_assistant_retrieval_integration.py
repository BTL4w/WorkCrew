"""Real PostgreSQL usage/checkpoint bridge, fenced by the Assistant job claim."""

from uuid import UUID

# pyright: reportPrivateUsage=false
import pytest

from app.core.database import create_session_factory
from tests.retrieval_fixtures import RetrievalHarness, pytestmark, retrieval_harness

__all__ = ["pytestmark", "retrieval_harness"]


@pytest.mark.asyncio
async def test_usage_checkpoints_cas_and_parent_charges(retrieval_harness: RetrievalHarness):
    from app.modules.assistant.adapters.retrieval_runtime import RetrievalUsageAdapter
    from work_management_ai.agents.retrieve.budget import RetrievalCheckpoint

    h = retrieval_harness
    usage = RetrievalUsageAdapter(
        budgets=h.service(), actor=h.actor, run_id=h.run_id, lease=h.lease()
    )
    assert 0 < await usage.remaining_seconds() <= 25
    assert await usage.load_checkpoint() is None
    checkpoint = RetrievalCheckpoint(binding="a" * 64, version=1)
    await usage.save_checkpoint(checkpoint)
    assert await usage.load_checkpoint() == checkpoint
    with pytest.raises(ValueError, match="CHECKPOINT"):
        await usage.save_checkpoint(checkpoint)
    await usage.reserve_node("node:1:validate")
    state = await h.service().start_run(h.actor, h.run_id, h.lease())
    assert state.reserved["nodes"] == 1
    receipt = await usage.reserve_model_attempt(attempt_no=1, input_tokens=100, output_tokens=200)
    await usage.record_model_usage(receipt, None, safe_code="MODEL_TIMEOUT")
    state = await h.service().start_run(h.actor, h.run_id, h.lease())
    assert state.reserved["model_attempts"] == 1
    await h.sql("UPDATE assistant_jobs SET locked_by='other' WHERE id=:id", {"id": h.job_id})
    with pytest.raises(ValueError):
        await usage.load_checkpoint()


@pytest.mark.asyncio
async def test_usage_rejects_foreign_tenant_and_disabled_gate(retrieval_harness: RetrievalHarness):
    from app.modules.assistant.adapters.retrieval_runtime import RetrievalUsageAdapter

    h = retrieval_harness
    usage = RetrievalUsageAdapter(
        budgets=h.service(), actor=h.foreign, run_id=h.run_id, lease=h.lease()
    )
    with pytest.raises(ValueError):
        await usage.remaining_seconds()
    h.enabled = False
    usage = RetrievalUsageAdapter(
        budgets=h.service(), actor=h.actor, run_id=h.run_id, lease=h.lease()
    )
    with pytest.raises(ValueError):
        await usage.remaining_seconds()


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["vi", "en"])
async def test_actual_turn_executor_routes_and_replays_without_new_provider_cost(
    retrieval_harness: RetrievalHarness, locale: str
):
    from app.core.config import Settings
    from app.modules.assistant.adapters.agent_runtime import (
        AssistantTurnExecutor,
        build_agent_registry,
        build_execution_engine_factory,
    )
    from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory
    from app.modules.identity.adapters.auth_repository import SqlAlchemyAuthTransactionFactory
    from app.modules.identity.adapters.current_actor import CurrentActorResolver
    from app.modules.identity.application.current_actor_service import CurrentActorService
    from app.modules.retrieval.adapters.composition import build_retrieval_runtime
    from app.modules.retrieval.adapters.transaction import RetrievalActivationPolicy
    from work_management_ai.model_gateway.mock import MockModelGateway

    h = retrieval_harness
    settings = Settings(
        environment="test",
        retrieval_enabled=True,
        retrieval_provider="mock",
        retrieval_core_exit_verified=True,
    )
    sessions = create_session_factory(h.engine)
    actors = CurrentActorResolver(CurrentActorService(SqlAlchemyAuthTransactionFactory(sessions)))
    transactions = PostgreSQLAssistantTransactionFactory(sessions)
    activation = RetrievalActivationPolicy(lambda: settings).context()
    registry, tools = build_agent_registry(activation=activation)
    runtime = build_retrieval_runtime(
        settings=settings, sessions=sessions, actors=actors, tools=tools, transactions=transactions
    )
    await h.sql(
        "UPDATE assistant_conversations SET last_message_sequence=1 WHERE id=:id",
        {"id": h.job.conversation_id},
    )
    message = "Search public web for public planning"
    await h.sql(
        "UPDATE assistant_turns SET objective=:message, locale=:locale WHERE id=:id",
        {"id": h.job.turn_id, "message": message, "locale": locale},
    )
    plan = dict(
        objectives=["public guidance"],
        response_language=locale,
        steps=[
            dict(
                step_id="web-actual",
                target_agent_id="retrieve",
                target_agent_version="1.0.0",
                capability="retrieval.search_web",
                objective="public guidance",
                typed_input=dict(
                    search_queries=["public planning"],
                    message_spans=[dict(start=22, end=37, text="public planning")],
                    requirements=[dict(requirement_id="topic", description="public planning")],
                    source_mode="WEB",
                    answer_mode="EVIDENCE_ONLY",
                ),
                mode="READ_ONLY",
            )
        ],
    )
    gateway = MockModelGateway(fixtures={f"orchestrator.{locale}.plan": plan})
    factory = build_execution_engine_factory(
        model_gateway=gateway,
        registry=registry,
        actor_resolver=actors,
        work_tool_executor=runtime,
        transaction_factory=transactions,
        retrieval_tools=runtime,
        retrieval_usage=runtime.usage,
        activation=activation,
    )
    executor = AssistantTurnExecutor(
        transaction_factory=transactions,
        registry=registry,
        engine_factory=factory,
        retrieval_runtime=runtime,
        activation=activation,
    )
    await executor.execute_job(job=h.job, actor=h.actor)
    result = (
        (
            await h.sql(
                "SELECT typed_output FROM agent_runs WHERE orchestration_run_id=:id "
                "AND agent_id='retrieve' AND capability='retrieval.search_web' "
                "AND typed_output IS NOT NULL",
                {"id": h.orchestration_id},
            )
        )
        .mappings()
        .all()
    )
    assert len(result) == 1
    assert result[0]["typed_output"]["typed_output"]["evidence"]
    count = (
        await h.sql(
            "SELECT count(*) FROM retrieval_attempt_reservations WHERE organization_id=:org",
            {"org": h.actor.organization_id},
        )
    ).scalar_one()
    await executor.execute_job(job=h.job, actor=h.actor)
    assert (
        await h.sql(
            "SELECT count(*) FROM retrieval_attempt_reservations WHERE organization_id=:org",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == count


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["transient", "overrun"])
async def test_transient_model_retry_uses_same_canonical_budget(
    retrieval_harness: RetrievalHarness,
    mode: str,
):
    from pydantic import BaseModel

    from app.core.config import Settings
    from app.modules.assistant.adapters.agent_runtime import CurrentAgentActorResolver
    from app.modules.assistant.adapters.retrieval_runtime import RetrievalUsageAdapter
    from app.modules.planning_runs.adapters.ai_runtime import build_model_gateway
    from tests.test_retrieval_replay_integration import _executor_parts
    from work_management_ai.agents.retrieve.harness import RetrieveHarness
    from work_management_ai.model_gateway.contracts import (
        StructuredModelRequest,
        StructuredModelResponse,
    )
    from work_management_ai.model_gateway.errors import ModelTimeoutError
    from work_management_ai.runtime.activation import ActivationContext
    from work_management_ai.runtime.contracts import AgentHandoff

    h = retrieval_harness
    tools, backend = _executor_parts(h)
    gateway = build_model_gateway(Settings(environment="test", ai_provider="mock"))

    class TransientModel:
        attempts = 0

        async def generate_structured[T: BaseModel](
            self, request: StructuredModelRequest[T]
        ) -> StructuredModelResponse[T]:
            self.attempts += 1
            if mode == "transient" and self.attempts == 1:
                raise ModelTimeoutError("synthetic timeout")
            response = await gateway.generate_structured(request)
            if mode == "overrun":
                from dataclasses import replace

                from work_management_ai.model_gateway.contracts import ModelUsage

                return replace(response, usage=ModelUsage(input_tokens=50000, output_tokens=1))
            return response

    model = TransientModel()
    from uuid import NAMESPACE_URL, uuid5

    handoff = AgentHandoff.model_validate(
        dict(
            orchestration_run_id=h.orchestration_id,
            parent_agent_run_id=uuid5(NAMESPACE_URL, f"orchestrator:{h.job.turn_id}"),
            target_agent_id="retrieve",
            target_agent_version="1.0.0",
            capability="retrieval.search_web",
            objective="Public guidance",
            typed_input=dict(
                query="public planning",
                locale="en",
                requirements=[dict(requirement_id="topic", description="Planning")],
            ),
            context_references=[],
            actor=dict(
                organization_id=h.actor.organization_id, membership_id=h.actor.membership_id
            ),
            budget=dict(
                max_model_attempts=2,
                max_input_tokens=12000,
                max_output_tokens=3200,
                max_iterations=12,
                max_tool_calls=8,
                timeout_seconds=25,
            ),
            step_id="retrieve-1",
            idempotency_key=f"{h.job.turn_id}:retrieve-1",
        )
    )
    result = await RetrieveHarness(
        model_gateway=model,
        tool_executor=tools,
        usage_port=RetrievalUsageAdapter(
            budgets=h.service(), actor=h.actor, run_id=h.run_id, lease=h.lease()
        ),
        actor_resolver=CurrentAgentActorResolver(backend._actors),
        activation=ActivationContext(5, True, frozenset({"I3"}), True),
    ).run(handoff)
    assert model.attempts == (2 if mode == "transient" else 1)
    assert result.typed_output["outcome"] == (
        "ANSWER_READY" if mode == "transient" else "EVIDENCE_READY"
    )
    assert result.evidence
    if mode == "overrun":
        from work_management_ai.agents.retrieve.contracts import RetrieveOutput

        assert RetrieveOutput.model_validate(result.typed_output).usage.input_tokens == 50000
    state = await h.service().start_run(h.actor, h.run_id, h.lease())
    assert state.reserved["model_attempts"] == (2 if mode == "transient" else 1)


@pytest.mark.asyncio
async def test_protected_project_name_is_refused_even_in_current_public_message(
    retrieval_harness: RetrievalHarness,
):
    from uuid import uuid4

    h = retrieval_harness
    await h.sql(
        "INSERT INTO projects(id,organization_id,name,created_by_membership_id,"
        "updated_by_membership_id) VALUES(:id,:org,'Project Orion',:member,:member)",
        {"id": uuid4(), "org": h.actor.organization_id, "member": h.actor.membership_id},
    )
    message = "Search public web for Project Orion"
    intent = dict(
        search_queries=["Project Orion"],
        message_spans=[dict(start=22, end=35, text="Project Orion")],
    )
    with pytest.raises(ValueError, match="PROTECTED"):
        await h.service().validate_public_intent(h.actor, intent, message)
    assert (
        await h.sql(
            "SELECT count(*) FROM retrieval_attempt_reservations WHERE organization_id=:org",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 0


@pytest.mark.asyncio
async def test_api_projection_rechecks_source_ownership_and_gate(
    retrieval_harness: RetrievalHarness,
):
    from app.modules.assistant.adapters.retrieval_projection import RetrievalBlockProjector
    from tests.test_retrieval_replay_integration import _executor_parts, _request

    h = retrieval_harness
    executor, _ = _executor_parts(h)
    result = await executor.execute(_request(h))
    assert result.status == "SUCCEEDED"
    block = dict(
        kind="work_evidence",
        summary="Verified source",
        evidence=[
            dict(
                evidence_id=str(ref.resource_id),
                resource_type="WEB_SOURCE_SNAPSHOT",
                resource_id=str(ref.resource_id),
                version=1,
            )
            for ref in result.evidence
        ],
    )
    projector = RetrievalBlockProjector(h.service())
    assert await projector.project(h.actor, block) == block
    assert (await projector.project(h.foreign, block))["kind"] == "text"
    h.enabled = False
    assert (await projector.project(h.actor, block))["kind"] == "text"
    event = await projector.project(h.actor, {"content_blocks": [block]})
    assert event["content_blocks"][0]["kind"] == "text"


@pytest.mark.asyncio
async def test_final_revalidation_rejection_cannot_return_unchecked_result(
    retrieval_harness: RetrievalHarness,
):
    from app.modules.assistant.adapters.agent_runtime import build_agent_registry
    from app.modules.assistant.adapters.execution_recorder import PostgreSQLExecutionRecorder
    from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory
    from work_management_ai.runtime.contracts import (
        ActorReference,
        AgentId,
        AgentResult,
        AgentRunStatus,
    )

    h = retrieval_harness

    class RefusedConsumption:
        async def revalidate_agent_result(
            self, run_id: UUID, reference: ActorReference, result: AgentResult
        ) -> AgentResult:
            return AgentResult(
                agent_id=AgentId.RETRIEVE,
                agent_version="1.0.0",
                status=AgentRunStatus.FAILED,
                typed_output={},
                stop_reason="RETRIEVAL_REPLAY_REJECTED",
                safe_error_code="RETRIEVAL_CAPABILITY_INACTIVE",
            )

    registry, _ = build_agent_registry()
    recorder = PostgreSQLExecutionRecorder(
        transaction_factory=PostgreSQLAssistantTransactionFactory(create_session_factory(h.engine)),
        registry=registry,
        job=h.job,
        actor=h.actor,
        retrieval_replay_validator=RefusedConsumption(),
    )
    result = AgentResult(
        agent_id=AgentId.RETRIEVE,
        agent_version="1.0.0",
        status=AgentRunStatus.COMPLETED,
        typed_output={},
        stop_reason="NOT_FOUND",
    )
    with pytest.raises(ValueError, match="RETRIEVAL_RESULT_REJECTED"):
        await recorder.finish_agent_run(h.run_id, result)
    assert (
        await h.sql("SELECT status FROM agent_runs WHERE id=:id", {"id": h.run_id})
    ).scalar_one() == "FAILED"
