"""Planning accepts only canonical, current source bindings and remains proposal-only."""

# pyright: reportPrivateUsage=false
from dataclasses import replace

import pytest

from tests.retrieval_fixtures import RetrievalHarness, pytestmark, retrieval_harness
from tests.test_retrieval_replay_integration import _executor_parts, _request

__all__ = ["pytestmark", "retrieval_harness"]


@pytest.mark.asyncio
async def test_planning_hydrates_snapshot_context_and_blocks_expiry(
    retrieval_harness: RetrievalHarness,
):
    from app.core.database import create_session_factory
    from app.modules.assistant.adapters.retrieval_planning import PlanningRetrievalContext
    from app.modules.organization.domain.roles import MembershipRole

    h = retrieval_harness
    executor, _ = _executor_parts(h)
    result = await executor.execute(_request(h))
    assert result.status == "SUCCEEDED"
    source = result.evidence[0]
    # Bind a real Planning Agent Run to a workflow using the existing FK/application path.
    from uuid import uuid4

    from work_management_ai.agents.planning.contracts import PlanningAgentInput

    value = PlanningAgentInput.model_validate(
        dict(
            operation="CREATE",
            locale="en",
            brief="Plan a public training event",
            retrieval_context_refs=[
                {"evidence_id": source.resource_id, "fingerprint": source.fingerprint}
            ],
        )
    )
    # Before canonical linkage no user/model-provided ref grants access.
    context = PlanningRetrievalContext(
        sessions=create_session_factory(h.engine), budgets=h.service()
    )
    with pytest.raises(ValueError):
        await context.load_bound(h.actor, uuid4(), value.retrieval_context_refs)
    await h.sql("UPDATE memberships SET role='MANAGER' WHERE id=:id", {"id": h.actor.membership_id})
    manager = replace(h.actor, role=MembershipRole.MANAGER)
    # The source itself is still owned by this membership; hash/expiry are mandatory.
    evidence = await context.resolve(manager, value.retrieval_context_refs)
    assert evidence["untrusted_web_evidence"][0]["fingerprint"] == source.fingerprint
    assert (
        evidence["untrusted_web_evidence"][0]["excerpt"]
        == "Public planning guide: review the goal and constraints."
    )
    with pytest.raises(ValueError):
        await context.resolve(h.foreign, value.retrieval_context_refs)
    await h.sql(
        "UPDATE memberships SET role='EMPLOYEE' WHERE id=:id", {"id": manager.membership_id}
    )
    with pytest.raises(ValueError):
        await context.resolve(manager, value.retrieval_context_refs)
    await h.sql("UPDATE memberships SET role='MANAGER' WHERE id=:id", {"id": manager.membership_id})
    from datetime import UTC, datetime, timedelta

    from app.modules.feedback.adapters.payload_repository import RetentionTransactions
    from app.modules.feedback.application.retention_service import RetentionService

    await RetentionService(RetentionTransactions(create_session_factory(h.engine))).purge_once(
        organization_id=h.actor.organization_id, now=datetime.now(UTC) + timedelta(days=31)
    )
    with pytest.raises(ValueError):
        await context.resolve(manager, value.retrieval_context_refs)
    assert (
        await h.sql(
            "SELECT count(*) FROM projects WHERE organization_id=:org",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 0


@pytest.mark.asyncio
async def test_explicit_research_then_planning_queues_evidence_without_business_writes(
    retrieval_harness: RetrievalHarness,
):
    import json

    from app.core.config import Settings
    from app.core.database import create_session_factory
    from app.modules.assistant.adapters.agent_runtime import (
        AssistantTurnExecutor,
        build_agent_registry,
        build_execution_engine_factory,
    )
    from app.modules.assistant.adapters.planning_tools import AssistantPlanningToolAdapter
    from app.modules.assistant.adapters.retrieval_planning import PlanningRetrievalContext
    from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory
    from app.modules.assistant.adapters.work_tools import RecordingToolExecutor
    from app.modules.identity.adapters.auth_repository import SqlAlchemyAuthTransactionFactory
    from app.modules.identity.adapters.current_actor import CurrentActorResolver
    from app.modules.identity.application.current_actor_service import CurrentActorService
    from app.modules.organization.domain.roles import MembershipRole
    from app.modules.planning_runs.adapters.ai_runtime import PlanningAIRuntime
    from app.modules.planning_runs.adapters.transaction import (
        PostgreSQLPlanningRunTransactionFactory,
    )
    from app.modules.planning_runs.application.proposal_service import ProposalService
    from app.modules.planning_runs.application.run_service import PlanningRunService
    from app.modules.retrieval.adapters.composition import build_retrieval_runtime
    from app.modules.retrieval.adapters.transaction import RetrievalActivationPolicy
    from work_management_ai.model_gateway.mock import MockModelGateway

    h = retrieval_harness
    await h.sql("UPDATE memberships SET role='MANAGER' WHERE id=:id", {"id": h.actor.membership_id})
    actor = replace(h.actor, role=MembershipRole.MANAGER)
    message = "Search public web for public planning and create a plan to launch a training course"
    await h.sql(
        "UPDATE assistant_conversations SET last_message_sequence=1 WHERE id=:id",
        {"id": h.job.conversation_id},
    )
    await h.sql(
        "UPDATE assistant_turns SET objective=:message WHERE id=:id",
        {"id": h.job.turn_id, "message": message},
    )
    await h.sql(
        "UPDATE assistant_messages SET content_blocks=CAST(:blocks AS jsonb) WHERE id=:id",
        {"id": h.message_id, "blocks": json.dumps([{"kind": "text", "text": message}])},
    )
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
    evidence = PlanningRetrievalContext(sessions=sessions, budgets=runtime.budgets)
    planning_transactions = PostgreSQLPlanningRunTransactionFactory(sessions)
    planning_runtime = PlanningAIRuntime(progressive_enabled=True)
    planning_tools = RecordingToolExecutor(
        transaction_factory=transactions,
        tool_registry=tools,
        backend=AssistantPlanningToolAdapter(
            actor_resolver=actors,
            assistant_transaction_factory=transactions,
            planning_run_service=PlanningRunService(
                transaction_factory=planning_transactions, runtime=planning_runtime
            ),
            proposal_service=ProposalService(
                transaction_factory=planning_transactions, runtime=planning_runtime
            ),
            retrieval_context=evidence,
        ),
    )
    plan = dict(
        objectives=["Research and plan a training course"],
        response_language="en",
        planning_intent="START_NEW",
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
            ),
            dict(
                step_id="plan-actual",
                target_agent_id="planning",
                target_agent_version="1.0.0",
                capability="planning.create",
                objective="Launch a training course",
                typed_input=dict(operation="CREATE", locale="en", brief="Launch a training course"),
                depends_on=["web-actual"],
                mode="PROPOSAL",
            ),
        ],
    )
    gateway = MockModelGateway(fixtures={"orchestrator.en.plan": plan})
    factory = build_execution_engine_factory(
        model_gateway=gateway,
        registry=registry,
        actor_resolver=actors,
        work_tool_executor=runtime,
        transaction_factory=transactions,
        planning_tool_executor=planning_tools,
        retrieval_tools=runtime,
        retrieval_usage=runtime.usage,
        activation=activation,
        public_query_validator=runtime,
    )
    executor = AssistantTurnExecutor(
        transaction_factory=transactions,
        registry=registry,
        engine_factory=factory,
        retrieval_runtime=runtime,
        activation=activation,
        progressive_planning_enabled=True,
    )
    await executor.execute_job(job=h.job, actor=actor)
    runs = (
        (
            await h.sql(
                "SELECT workflow_run_id,typed_input FROM agent_runs "
                "WHERE orchestration_run_id=:id AND agent_id='planning'",
                {"id": h.orchestration_id},
            )
        )
        .mappings()
        .all()
    )
    assert len(runs) == 1
    assert runs[0]["typed_input"]["retrieval_context_refs"]
    assert runs[0]["workflow_run_id"] is not None
    facts = await evidence.load(actor, runs[0]["workflow_run_id"])
    assert facts["untrusted_web_evidence"][0]["method"] in {"SEARCH_SNIPPET", "PAGE_CONTENT"}
    assert (
        await h.sql(
            "SELECT count(*) FROM projects WHERE organization_id=:org",
            {"org": actor.organization_id},
        )
    ).scalar_one() == 0
    assert (
        await h.sql(
            "SELECT count(*) FROM tasks WHERE organization_id=:org", {"org": actor.organization_id}
        )
    ).scalar_one() == 0
