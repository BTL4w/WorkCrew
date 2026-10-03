"""Hub dispatch and employee-owned Daily Update runtime contract."""

from uuid import uuid4

import pytest

from tests.test_evidence_api_integration import Harness
from tests.test_evidence_api_integration import harness as harness

__all__ = ["harness"]

from app.modules.assistant.adapters.agent_runtime import build_agent_registry
from work_management_ai.agents.daily_update.contracts import DailyUpdateHandoff
from work_management_ai.agents.orchestrator.contracts import (
    ActiveConversationContext,
    OrchestratorInput,
)
from work_management_ai.agents.orchestrator.harness import OrchestratorHarness
from work_management_ai.model_gateway.mock import MockModelGateway
from work_management_ai.runtime.contracts import (
    ActorReference,
    AgentHandoff,
    AgentId,
    AgentResult,
    AgentRunStatus,
    JsonValue,
    ResolvedActorContext,
)
from work_management_ai.runtime.policy_guard import PolicyGuard


class Actors:
    async def resolve(self, reference: ActorReference) -> ResolvedActorContext:
        return ResolvedActorContext(**reference.model_dump(), role="EMPLOYEE", is_active=True)


class Specialists:
    def __init__(self):
        self.handoffs: list[AgentHandoff] = []
        self.draft_id = uuid4()

    async def run_specialist(self, handoff: AgentHandoff) -> AgentResult:
        self.handoffs.append(handoff)
        return AgentResult(
            agent_id=AgentId.DAILY_UPDATE,
            agent_version="1.0.0",
            status=AgentRunStatus.AWAITING_HUMAN,
            typed_output={
                "draft_id": str(self.draft_id),
                "draft_version": 1,
                "task_id": handoff.typed_input["task_id"],
                "task_version": 2,
                "assessment_id": None,
                "needs_owner_confirmation": True,
            },
            stop_reason="AWAIT_OWNER",
        )


def test_daily_update_registry_is_phase_four_only():
    registry, tools = build_agent_registry()
    with pytest.raises(ValueError, match="AGENT_PHASE_INACTIVE"):
        registry.resolve(AgentId.DAILY_UPDATE, "1.0.0", 3)
    assert (
        registry.resolve(AgentId.DAILY_UPDATE, "1.0.0", 4).manifest.runtime.max_model_attempts == 3
    )
    assert tools.resolve("daily_update.prepare@1").manifest.risk_level.value == "PROPOSAL_ONLY"


@pytest.mark.asyncio
async def test_orchestrator_dispatches_daily_report_and_returns_owner_card_without_synthesis():
    registry, _ = build_agent_registry()
    specialists = Specialists()
    report = DailyUpdateHandoff(
        text="Đã khảo sát 50%", locale="vi", task_id=uuid4(), task_version=2
    )
    harness = OrchestratorHarness(
        model_gateway=MockModelGateway(fixtures={}),
        registry=registry,
        policy_guard=PolicyGuard(),
        actor_resolver=Actors(),
        specialists=specialists,
    )
    value = OrchestratorInput(
        conversation_id=uuid4(),
        turn_id=uuid4(),
        message=report.text,
        locale="vi",
        actor=ActorReference(organization_id=uuid4(), membership_id=uuid4()),
        active_context=ActiveConversationContext(recent_messages=(), daily_update=report),
    )
    result = await harness.run_turn(value)
    assert len(specialists.handoffs) == 1
    assert specialists.handoffs[0].target_agent_id is AgentId.DAILY_UPDATE
    assert result.blocks[0].kind == "daily_update"
    assert result.status.value == "AWAITING_HUMAN"
    assert result.blocks[0].draft_id == specialists.draft_id


@pytest.mark.integration
@pytest.mark.skipif(
    __import__("os").getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"
)
@pytest.mark.asyncio
async def test_text_and_original_prepare_draft_zero_facts_then_owner_confirms_once(
    harness: Harness,
):
    from uuid import UUID

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.config import Settings
    from app.modules.assistant.adapters.agent_runtime import CurrentAgentActorResolver
    from app.modules.assistant.adapters.daily_update_tools import DailyUpdateToolAdapter
    from app.modules.identity.domain.auth import AuthenticatedActor
    from app.modules.progress.adapters.daily_update_runtime import build_daily_services
    from app.modules.progress.domain.daily_updates import ConfirmDailyUpdateCommand
    from tests.test_daily_update_api_integration import daily_app, seed_task
    from tests.test_evidence_api_integration import upload
    from work_management_ai.agents.daily_update.harness import DailyUpdateHarness
    from work_management_ai.runtime.budgeted_gateway import BudgetedDailyGateway
    from work_management_ai.runtime.contracts import AgentBudget, AgentHandoff
    from work_management_ai.runtime.daily_update_budget import BudgetScope, daily_model_scope

    task = UUID(await seed_task(harness))
    app = daily_app(harness)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        proof = (await upload(client)).json()
    refs: list[JsonValue] = [{"evidence_id": str(proof["evidence_id"]), "version": 1}]
    gateway = MockModelGateway(
        fixtures={
            "daily_update.en.extract": {
                "reported_percent": "50",
                "remaining_hours": None,
                "spent_hours": None,
                "done_text": "Completed survey",
                "blockers": [{"text": "Awaiting materials", "severity": "HIGH"}],
                "next_steps": "",
                "needs_clarification": False,
            },
            "daily_update.claims": {
                "claims": [
                    {
                        "source_claim_id": "0:0",
                        "text": "Completed survey",
                        "category": "WORK",
                        "checkability": True,
                    }
                ]
            },
            "daily_update.compare": {
                "findings": [
                    {
                        "claim_id": "c0",
                        "finding": "SUPPORTED",
                        "source_refs": refs,
                        "limitation": "",
                    }
                ]
            },
        }
    )
    sessions = async_sessionmaker(
        bind=harness.connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    updates, assessments, usage = build_daily_services(
        sessions=sessions,
        settings=Settings(ai_provider="mock"),
        evidence=harness.service,
        gateway=gateway,
    )

    class CurrentActors:
        async def resolve(
            self, *, organization_id: UUID, membership_id: UUID
        ) -> AuthenticatedActor | None:
            return (
                harness.actor
                if membership_id == harness.actor.membership_id
                and organization_id == harness.actor.organization_id
                else None
            )

    actors = CurrentActors()
    runtime = DailyUpdateHarness(
        model_gateway=BudgetedDailyGateway(gateway, usage, image_token_bound=6000),
        tool_executor=DailyUpdateToolAdapter(
            actors=actors, updates=updates, assessments=assessments
        ),
        actor_resolver=CurrentAgentActorResolver(actors),
    )
    scope = BudgetScope(
        organization_id=harness.actor.organization_id,
        membership_id=harness.actor.membership_id,
        run_id=uuid4(),
    )
    with daily_model_scope(scope):
        result = await runtime.run(
            AgentHandoff(
                orchestration_run_id=uuid4(),
                parent_agent_run_id=uuid4(),
                target_agent_id=AgentId.DAILY_UPDATE,
                target_agent_version="1.0.0",
                capability="daily_update.prepare",
                objective="prepare my own daily update",
                typed_input={
                    "text": "Completed survey 50%",
                    "locale": "en",
                    "task_id": str(task),
                    "task_version": 1,
                    "evidence_refs": refs,
                },
                context_references=(),
                actor=ActorReference(
                    organization_id=scope.organization_id, membership_id=scope.membership_id
                ),
                budget=AgentBudget(max_iterations=8, max_tool_calls=12, timeout_seconds=180),
                step_id="daily-update",
                idempotency_key=str(uuid4()),
            )
        )
    assert result.status is AgentRunStatus.AWAITING_HUMAN, result
    count = (
        await harness.sql(
            "SELECT count(*) FROM task_progress_observations WHERE organization_id=:org",
            {"org": scope.organization_id},
        )
    ).scalar_one()
    assert count == 0
    assert (
        await harness.sql(
            "SELECT count(*) FROM blockers WHERE organization_id=:org",
            {"org": scope.organization_id},
        )
    ).scalar_one() == 0
    row = (
        await harness.sql(
            "SELECT attempts,input_tokens,output_tokens FROM agent_usage_budgets "
            "WHERE resource_id=:id AND organization_id=:org",
            {"id": scope.run_id, "org": scope.organization_id},
        )
    ).one()
    assert row.attempts == 3 and row.input_tokens <= 24000 and row.output_tokens == 4000
    draft_id = UUID(str(result.typed_output["draft_id"]))
    assessment = await assessments.get_current(harness.actor, draft_id)
    assert assessment.state == "READY", assessment
    command = ConfirmDailyUpdateCommand(
        draft_id=draft_id, expected_draft_version=1, assessment_id=assessment.id
    )
    from app.modules.progress.domain.daily_updates import DailyUpdateError

    with pytest.raises(DailyUpdateError):
        await updates.confirm(harness.peer, command, str(uuid4()), "non-owner")
    key = str(uuid4())
    confirmed = await updates.confirm(harness.actor, command, key, "owner")
    replayed = await updates.confirm(harness.actor, command, key, "owner-retry")
    assert confirmed == replayed
    assert (
        await harness.sql(
            "SELECT count(*) FROM blockers WHERE organization_id=:org",
            {"org": scope.organization_id},
        )
    ).scalar_one() == 1
    assert len(confirmed.observations) == 1
    count = (
        await harness.sql(
            "SELECT count(*) FROM task_progress_observations WHERE organization_id=:org",
            {"org": scope.organization_id},
        )
    ).scalar_one()
    assert count == 1


@pytest.mark.integration
@pytest.mark.skipif(
    __import__("os").getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"
)
@pytest.mark.asyncio
async def test_usage_is_durable_and_cross_tenant_rls_hides_counters(harness: Harness):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.modules.assistant.adapters.daily_update_usage import SqlAlchemyDailyUsageStore
    from work_management_ai.runtime.daily_update_budget import BudgetScope, UsageLimitExceeded

    sessions = async_sessionmaker(
        bind=harness.connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    store = SqlAlchemyDailyUsageStore(sessions)
    scope = BudgetScope(
        organization_id=harness.actor.organization_id,
        membership_id=harness.actor.membership_id,
        run_id=uuid4(),
    )
    for _ in range(3):
        await store.reserve(scope, input_tokens=100, output_tokens=100)
    with pytest.raises(UsageLimitExceeded):
        await SqlAlchemyDailyUsageStore(sessions).reserve(
            scope, input_tokens=100, output_tokens=100
        )
    async with sessions.begin() as session:
        await session.execute(text("SET LOCAL ROLE app_runtime"))
        await session.execute(
            text(
                "SELECT set_config('app.organization_id',:org,true),"
                "set_config('app.membership_id',:member,true)"
            ),
            {
                "org": str(harness.foreign.organization_id),
                "member": str(harness.foreign.membership_id),
            },
        )
        assert (
            await session.scalar(
                text("SELECT count(*) FROM agent_usage_budgets WHERE resource_id=:id"),
                {"id": scope.run_id},
            )
            == 0
        )


@pytest.mark.asyncio
async def test_checkpoint_replay_returns_waiting_daily_card_without_resuming_model():
    from contextlib import AbstractAsyncContextManager
    from typing import Self, cast
    from uuid import NAMESPACE_URL, UUID, uuid5

    from app.modules.assistant.adapters.agent_runtime import PostgreSQLExecutionRecorder
    from app.modules.assistant.application.ports import AssistantTransactionFactory
    from app.modules.assistant.domain.models import AgentRun, AssistantJob
    from app.modules.assistant.domain.models import AgentRunStatus as DomainStatus
    from work_management_ai.runtime.contracts import AgentBudget

    org, turn, orchestration = uuid4(), uuid4(), uuid4()
    actor = ActorReference(organization_id=org, membership_id=uuid4())
    handoff = AgentHandoff(
        orchestration_run_id=orchestration,
        parent_agent_run_id=uuid4(),
        target_agent_id=AgentId.DAILY_UPDATE,
        target_agent_version="1.0.0",
        capability="daily_update.prepare",
        objective="Prepare my report",
        typed_input={},
        context_references=(),
        actor=actor,
        budget=AgentBudget(max_iterations=8, max_tool_calls=12, timeout_seconds=180),
        step_id="daily-update",
        idempotency_key=f"{turn}:daily-update",
    )
    expected = AgentResult(
        agent_id=AgentId.DAILY_UPDATE,
        agent_version="1.0.0",
        status=AgentRunStatus.AWAITING_HUMAN,
        typed_output={
            "draft_id": str(uuid4()),
            "draft_version": 1,
            "task_id": str(uuid4()),
            "task_version": 1,
            "assessment_id": None,
            "needs_owner_confirmation": True,
        },
        stop_reason="AWAIT_OWNER",
    )
    waiting = (
        AgentRun.create(
            id=uuid5(NAMESPACE_URL, f"agent-run:{handoff.idempotency_key}"),
            organization_id=org,
            orchestration_run_id=orchestration,
            parent_agent_run_id=handoff.parent_agent_run_id,
            agent_id="daily_update",
            agent_version="1.0.0",
            manifest_fingerprint="f" * 64,
            capability="daily_update.prepare",
            typed_input={},
            budget={},
        )
        .mark_running()
        .mark_awaiting(
            status=DomainStatus.AWAITING_HUMAN,
            typed_output=expected.model_dump(mode="json"),
            stop_reason="AWAIT_OWNER",
        )
    )

    class Repository:
        resumed = False

        async def get_agent_run(self, *, organization_id: UUID, run_id: UUID) -> AgentRun:
            return waiting

        async def resume_agent_run(self, *, run: AgentRun) -> None:
            self.resumed = True

    repository = Repository()

    class Transaction(AbstractAsyncContextManager["Transaction"]):
        def __init__(self):
            self.repository = repository

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def commit(self) -> None:
            return None

    def transactions(context: object) -> Transaction:
        return Transaction()

    job = AssistantJob.create(
        organization_id=org,
        conversation_id=uuid4(),
        turn_id=turn,
        orchestration_run_id=orchestration,
        requester_membership_id=actor.membership_id,
        payload={},
    )
    registry, _ = build_agent_registry()
    recorder = PostgreSQLExecutionRecorder(
        transaction_factory=cast(AssistantTransactionFactory, transactions),
        registry=registry,
        job=job,
    )
    replay = await recorder.start_agent_run(handoff)
    assert not repository.resumed
    assert replay.replayed_result == expected


@pytest.mark.integration
@pytest.mark.skipif(
    __import__("os").getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"
)
@pytest.mark.asyncio
async def test_shared_original_keeps_every_task_context(harness: Harness):
    from httpx import ASGITransport, AsyncClient

    from app.modules.progress.application.assessment_service import ComparisonBudget, OriginalSource
    from app.modules.progress.domain.evidence_support import Claim
    from tests.test_daily_update_api_integration import body, draft, seed_task
    from tests.test_evidence_api_integration import upload
    from tests.test_evidence_assessment_integration import Comparison, assess, assessed_app

    class CapturingComparison(Comparison):
        sources: tuple[OriginalSource, ...] = ()

        async def compare(
            self,
            claims: tuple[Claim, ...],
            original_sources: tuple[OriginalSource, ...],
            budget: ComparisonBudget,
        ):
            self.sources = original_sources
            return await super().compare(claims, original_sources, budget)

    first, second = await seed_task(harness), await seed_task(harness)
    comparison = CapturingComparison()
    app = assessed_app(harness, comparison)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        proof = (await upload(client)).json()
        refs = [{"evidence_id": proof["evidence_id"], "version": 1}]
        created = await draft(
            client, [body(first, evidence_refs=refs), body(second, evidence_refs=refs)]
        )
        result = await assess(client, created)
    assert result["state"] == "READY"
    assert len(comparison.sources) == 1
    assert {str(context.task_id) for context in comparison.sources[0].task_contexts} == {
        first,
        second,
    }
