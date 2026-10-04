"""Closure spine: hub draft -> owner warning confirmation -> Manager risk/digest."""

import os
from uuid import uuid4

import pytest

from app.modules.assistant.adapters.agent_runtime import build_agent_registry
from tests.test_evidence_api_integration import Harness, harness
from work_management_ai.agents.daily_update.contracts import DailyUpdateHandoff, EvidenceRef
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
    JsonValue,
)
from work_management_ai.runtime.policy_guard import PolicyGuard

__all__ = ["harness"]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


@pytest.mark.asyncio
async def test_employee_warning_confirmation_reaches_manager_risk_and_digest_without_plan_write(
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
    from tests.test_daily_update_api_integration import daily_app
    from tests.test_evidence_api_integration import upload
    from tests.test_risk_replanning_integration import business, prepared
    from work_management_ai.agents.daily_update.harness import DailyUpdateHarness
    from work_management_ai.runtime.budgeted_gateway import BudgetedDailyGateway
    from work_management_ai.runtime.contracts import AgentHandoff
    from work_management_ai.runtime.daily_update_budget import BudgetScope, daily_model_scope

    task, project, _, manager, _, _ = await prepared(harness)
    before = await business(harness)
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
                "score": "92",
                "rationale": "The survey is supported by the selected image.",
                "recommendations": [],
                "findings": [
                    {
                        "claim_id": "c0",
                        "finding": "CONTRADICTED",
                        "source_refs": refs,
                        "limitation": "",
                    }
                ],
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
    calls: list[AgentHandoff] = []
    results: list[AgentResult] = []

    class Specialists:
        async def run_specialist(self, handoff: AgentHandoff) -> AgentResult:
            calls.append(handoff)
            assert handoff.target_agent_id is AgentId.DAILY_UPDATE
            return await runtime.run(handoff)

    report = DailyUpdateHandoff(
        text="Completed survey 50%",
        locale="en",
        task_id=task,
        task_version=1,
        evidence_refs=tuple(EvidenceRef.model_validate(ref) for ref in refs),
    )
    with daily_model_scope(scope):
        result = await OrchestratorHarness(
            model_gateway=MockModelGateway(fixtures={}),
            registry=build_agent_registry()[0],
            policy_guard=PolicyGuard(),
            actor_resolver=CurrentAgentActorResolver(actors),
            specialists=Specialists(),
        ).run_turn(
            OrchestratorInput(
                conversation_id=uuid4(),
                turn_id=uuid4(),
                message=report.text,
                locale="en",
                actor=ActorReference(
                    organization_id=scope.organization_id, membership_id=scope.membership_id
                ),
                active_context=ActiveConversationContext(recent_messages=(), daily_update=report),
            )
        )
    assert result.status.value == "AWAITING_HUMAN", (result, results)
    assert len(calls) == 1 and calls[0].target_agent_id is AgentId.DAILY_UPDATE
    card = next(block for block in result.blocks if block.kind == "daily_update")
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
    draft_id = card.draft_id
    assessment = await assessments.get_current(harness.actor, draft_id)
    assert assessment.state == "READY", assessment
    assert assessment.result is not None and assessment.result.score == 92
    assert {w.code for w in assessment.warnings} == {"CONTRADICTION"}
    command = ConfirmDailyUpdateCommand(
        draft_id=draft_id,
        expected_draft_version=1,
        assessment_id=assessment.id,
        warning_acknowledgments=tuple(w.id for w in assessment.warnings),
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

    assert await business(harness) == before
    from tests.test_phase4_context_permissions import reader
    from tests.test_risk_api_integration import setup

    _, risks, _, _ = await setup(harness)
    await risks.request_refresh(manager, task, str(uuid4()))
    assert await risks.run_once(manager)
    context = await reader(harness).read(manager, task)
    assert context.score == "83" and any(f.kind == "WARNING" for f in context.permitted_sources)
    from datetime import UTC, datetime

    from app.modules.automations.adapters.digest_repository import DigestTransactions
    from app.modules.automations.adapters.repository import ScheduleTransactions
    from app.modules.automations.application.digest_service import DigestService
    from app.modules.automations.application.schedule_service import ScheduleService
    from app.modules.automations.domain.digests import AuthorizedJobScope
    from app.modules.automations.domain.schedules import ScheduleCommand

    schedules = ScheduleService(ScheduleTransactions(sessions))
    draft = await schedules.preview(
        manager,
        ScheduleCommand(
            project_id=project,
            timezone="UTC",
            cutoff="00:00",
            weekdays=(1, 2, 3, 4, 5, 6, 7),
            recipients=(manager.membership_id,),
        ),
        0,
        str(uuid4()),
    )
    await schedules.confirm(manager, draft.id, 0, str(uuid4()))
    window = (await schedules.get(manager, project)).window
    assert window is not None
    digests = DigestService(DigestTransactions(sessions))
    snapshot = await digests.trigger(
        AuthorizedJobScope(actor=manager, at=datetime.now(UTC)), window.id, "CUTOFF"
    )
    assert any(
        s.kind == "REVIEW" and s.state == "EVIDENCE_WARNING_ACKNOWLEDGED" for s in snapshot.sources
    )
    assert any(s.kind == "BLOCKER" for s in snapshot.sources)
    assert any(s.kind == "RISK" for s in snapshot.sources)
    assert await business(harness) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["en", "vi"])
async def test_verified_risk_routes_through_hub_to_planning_proposal_only(
    harness: Harness, locale: str
):
    from typing import Literal, cast
    from uuid import UUID

    from app.modules.assistant.adapters.agent_runtime import CurrentAgentActorResolver
    from app.modules.assistant.adapters.risk_tools import RiskToolAdapter
    from app.modules.identity.domain.auth import AuthenticatedActor
    from app.modules.planning_runs.adapters.ai_runtime import PlanningAIRuntime
    from app.modules.planning_runs.application.proposal_service import ProposalService
    from tests.test_phase4_context_permissions import reader
    from tests.test_risk_replanning_integration import business, prepared
    from tests.test_weekly_baseline_integration import weekly_app
    from work_management_ai.agents.planning.contracts import PlanningAgentInput, PlanningAgentOutput
    from work_management_ai.agents.planning.harness import PlanningAgentHarness
    from work_management_ai.agents.risk.harness import RiskHarness
    from work_management_ai.runtime.contracts import ToolExecutionRequest, ToolExecutionResult

    task, _, _, manager, binding, transactions = await prepared(harness)
    before = await business(harness)
    reads = reader(harness)
    context = await reads.read(manager, task)

    class CurrentActors:
        async def resolve(
            self, *, organization_id: UUID, membership_id: UUID
        ) -> AuthenticatedActor | None:
            return (
                manager
                if (organization_id, membership_id)
                == (manager.organization_id, manager.membership_id)
                else None
            )

    actors = CurrentActors()
    resolver = CurrentAgentActorResolver(actors)
    risk_tools = RiskToolAdapter(
        actors=actors, tasks=weekly_app(harness).state.task_service, reads=reads
    )
    source = context.observations[0].source_ids[0]
    risk = RiskHarness(
        model_gateway=MockModelGateway(
            fixtures={
                f"risk.{locale}.explain": {
                    "observation_explanations": [
                        {
                            "text": "Review the recorded deadline",
                            "observation_ids": [context.observations[0].id],
                            "source_ids": [source],
                            "assertions": [
                                {
                                    "source_id": source,
                                    "field": "title",
                                    "value": next(
                                        s.values["title"]
                                        for s in context.permitted_sources
                                        if s.id == source
                                    ),
                                }
                            ],
                        }
                    ],
                    "limitations": [],
                    "recommendations": [],
                    "replan_requested": True,
                }
            }
        ),
        tool_executor=risk_tools,
        actor_resolver=resolver,
    )
    service = ProposalService(transaction_factory=transactions, runtime=PlanningAIRuntime())

    # Typed port bridge to the real application transaction. Durable Assistant
    # tool/run linking is covered separately by Assistant and Playwright suites.
    class PlanningPort:
        async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
            assert request.tool_id == "planning.manage_run"
            assert request.actor.organization_id == manager.organization_id
            assert request.actor.membership_id == manager.membership_id
            value = PlanningAgentInput.model_validate(request.typed_input)
            assert value.risk_context is not None
            assert value.risk_context.model_dump(mode="json") == binding
            requested = await service.request_risk_revision(
                actor=manager,
                risk_context=value.risk_context.model_dump(mode="json"),
                instruction=value.manager_instruction or "",
                locale=value.locale,
                request_id=request.call_id,
                idempotency_key=request.idempotency_key,
            )
            output = PlanningAgentOutput(
                operation=value.operation,
                workflow_run_id=requested.run.id,
                workflow_status=requested.run.status.value,
                proposal_id=None,
                approval_id=None,
                awaiting="MANAGER_DECISION",
                public_summary="Queued for Manager review",
            )
            return ToolExecutionResult(
                status="SUCCEEDED",
                typed_output=cast(dict[str, JsonValue], output.model_dump(mode="json")),
            )

    planning = PlanningAgentHarness(
        model_gateway=MockModelGateway(fixtures={}),
        tool_executor=PlanningPort(),
        actor_resolver=resolver,
    )
    calls: list[AgentHandoff] = []
    results: list[AgentResult] = []

    class Specialists:
        async def run_specialist(self, handoff: AgentHandoff) -> AgentResult:
            calls.append(handoff)
            assert handoff.target_agent_id in {AgentId.RISK, AgentId.PLANNING}
            result = await (risk if handoff.target_agent_id is AgentId.RISK else planning).run(
                handoff
            )
            results.append(result)
            return result

    plan: dict[str, object] = {
        "objectives": ["Replan risk"],
        "steps": [
            {
                "step_id": "risk",
                "target_agent_id": "risk",
                "target_agent_version": "1.0.0",
                "capability": "risk.explain",
                "objective": "Explain risk",
                "typed_input": {"task_reference": str(task)},
                "depends_on": [],
                "mode": "READ_ONLY",
            }
        ],
        "unavailable_capabilities": [],
        "response_language": locale,
    }
    result = await OrchestratorHarness(
        model_gateway=MockModelGateway(fixtures={f"orchestrator.{locale}.plan": plan}),
        registry=build_agent_registry()[0],
        policy_guard=PolicyGuard(),
        actor_resolver=resolver,
        specialists=Specialists(),
    ).run_turn(
        OrchestratorInput(
            conversation_id=uuid4(),
            turn_id=uuid4(),
            message="Điều chỉnh kế hoạch tuần do rủi ro"
            if locale == "vi"
            else "Replan the weekly delivery due to risk",
            locale=cast(Literal["en", "vi"], locale),
            actor=ActorReference(
                organization_id=manager.organization_id, membership_id=manager.membership_id
            ),
            active_context=ActiveConversationContext(recent_messages=()),
        )
    )
    assert [c.target_agent_id for c in calls] == [AgentId.RISK, AgentId.PLANNING]
    assert result.status.value == "AWAITING_HUMAN", (result, results)
    assert await business(harness) == before
    assert (
        await harness.sql(
            "SELECT count(*) FROM workflow_jobs WHERE organization_id=:org "
            "AND job_type='proposal.risk_replan'",
            {"org": manager.organization_id},
        )
    ).scalar_one() == 1
