"""Phase 3 Assignment runtime composition tests."""

from contextlib import AbstractAsyncContextManager
from typing import Self, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from pydantic import ValidationError

from app.modules.assistant.adapters.agent_runtime import (
    PostgreSQLExecutionRecorder,
    build_agent_registry,
    resolve_ambient_team_context,
)
from app.modules.assistant.api.schemas import CardAction, MessageResponse
from app.modules.assistant.application.ports import AssistantTransactionFactory
from app.modules.assistant.domain.models import (
    AgentRun,
    AssistantJob,
    AssistantMessage,
    MessageRole,
)
from app.modules.assistant.domain.models import (
    AgentRunStatus as DomainAgentRunStatus,
)
from work_management_ai.agents.orchestrator.contracts import PendingFollowup
from work_management_ai.runtime.contracts import (
    ActorReference,
    AgentBudget,
    AgentHandoff,
    AgentId,
    AgentRunStatus,
)


def test_runtime_registry_activates_assignment_only_in_phase_three() -> None:
    registry, tools = build_agent_registry()

    with pytest.raises(ValueError, match="AGENT_PHASE_INACTIVE"):
        registry.resolve(AgentId.ASSIGNMENT, "1.0.0", active_phase=2)
    registered = registry.resolve(AgentId.ASSIGNMENT, "1.0.0", active_phase=3)
    assert registered.manifest.agent.id is AgentId.ASSIGNMENT
    assert tools.resolve("assignment.manage_team@1").manifest.name == "assignment.manage_team"


def test_team_revise_action_requires_exact_recommendation_version() -> None:
    action = CardAction(
        kind="TEAM_REVISE",
        recommendation_id=uuid4(),
        recommendation_version=2,
    )

    assert action.recommendation_version == 2
    with pytest.raises(ValidationError):
        CardAction(kind="TEAM_REVISE", recommendation_id=uuid4())


@pytest.mark.parametrize(
    "block",
    [
        {
            "kind": "team_recommendation",
            "project_id": str(uuid4()),
            "recommendation_id": str(uuid4()),
            "recommendation_version": 1,
            "status": "PROPOSED",
            "explanation_status": "UNAVAILABLE",
        },
        {
            "kind": "team_decision_result",
            "recommendation_id": str(uuid4()),
            "recommendation_version": 1,
            "decision": "APPROVE",
        },
        {
            "kind": "assignment_result",
            "task_id": str(uuid4()),
            "task_version": 3,
            "membership_id": str(uuid4()),
            "warning_codes": [],
        },
    ],
)
def test_assignment_public_blocks_are_strictly_serializable(block: dict[str, object]) -> None:
    message = AssistantMessage(
        id=uuid4(),
        organization_id=uuid4(),
        conversation_id=uuid4(),
        sequence=1,
        role=MessageRole.ASSISTANT,
        content_blocks=(block,),
    )

    assert MessageResponse.from_domain(message).content_blocks[0].kind == block["kind"]


def test_historical_combined_decision_cannot_restart_team_on_a_later_turn() -> None:
    workflow_id, project_id, proposal_id = uuid4(), uuid4(), uuid4()
    message = AssistantMessage(
        id=uuid4(),
        organization_id=uuid4(),
        conversation_id=uuid4(),
        sequence=1,
        role=MessageRole.ASSISTANT,
        content_blocks=(
            {
                "kind": "decision_result",
                "workflow_run_id": str(workflow_id),
                "decision": "APPROVE",
                "proposal_id": str(proposal_id),
                "proposal_version": 1,
                "project_id": str(project_id),
                "continue_team": True,
            },
        ),
    )

    assert resolve_ambient_team_context((message,)) is None

    active = resolve_ambient_team_context(
        (message,),
        pending_followup=PendingFollowup(
            planning_workflow_run_id=workflow_id,
            planning_proposal_id=proposal_id,
            planning_proposal_version=1,
            project_id=project_id,
            state="READY",
        ),
    )
    assert active is not None
    assert active.project_id == project_id
    assert active.requested_operation == "RECOMMEND_TEAM"


@pytest.mark.asyncio
async def test_waiting_assignment_agent_run_is_resumed_before_reexecution() -> None:
    organization_id, orchestration_run_id, turn_id = uuid4(), uuid4(), uuid4()
    handoff = AgentHandoff(
        orchestration_run_id=orchestration_run_id,
        parent_agent_run_id=uuid5(NAMESPACE_URL, f"orchestrator:{turn_id}"),
        target_agent_id=AgentId.ASSIGNMENT,
        target_agent_version="1.0.0",
        capability="assignment.recommend_team",
        objective="Recommend a team",
        typed_input={},
        context_references=(),
        actor=ActorReference(membership_id=uuid4(), organization_id=organization_id),
        budget=AgentBudget(max_iterations=2, max_tool_calls=1, timeout_seconds=10),
        step_id="recommend_team",
        idempotency_key="assignment-step",
    )
    waiting = (
        AgentRun.create(
            id=uuid5(NAMESPACE_URL, f"agent-run:{handoff.idempotency_key}"),
            organization_id=organization_id,
            orchestration_run_id=orchestration_run_id,
            parent_agent_run_id=handoff.parent_agent_run_id,
            agent_id="assignment",
            agent_version="1.0.0",
            manifest_fingerprint="f" * 64,
            capability=handoff.capability,
            typed_input={},
            budget={},
        )
        .mark_running()
        .mark_awaiting(
            status=DomainAgentRunStatus.AWAITING_HUMAN,
            typed_output={"status": "pending"},
            stop_reason="AWAITING_HUMAN",
        )
    )

    class Repository:
        resumed: AgentRun | None = None

        async def get_agent_run(self, **_):
            return waiting

        async def resume_agent_run(self, *, run: AgentRun) -> None:
            self.resumed = run

    repository = Repository()

    class Transaction(AbstractAsyncContextManager["Transaction"]):
        def __init__(self) -> None:
            self.repository = repository

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_):
            return None

        async def commit(self):
            return None

    job = AssistantJob.create(
        organization_id=organization_id,
        conversation_id=uuid4(),
        turn_id=turn_id,
        orchestration_run_id=orchestration_run_id,
        requester_membership_id=handoff.actor.membership_id,
        payload={},
    )
    registry, _ = build_agent_registry()

    def transaction_factory(context: object) -> Transaction:
        del context
        return Transaction()

    recorder = PostgreSQLExecutionRecorder(
        transaction_factory=cast(AssistantTransactionFactory, transaction_factory),
        registry=registry,
        job=job,
    )

    recorded = await recorder.start_agent_run(handoff)

    assert recorded.status is AgentRunStatus.RUNNING
    assert repository.resumed is not None
    assert repository.resumed.status is DomainAgentRunStatus.RUNNING
