"""Missing goals require input; short concrete goals can produce useful drafts."""

from typing import Literal
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.core.database import create_database_engine, create_session_factory
from app.modules.planning_runs.adapters.ai_runtime import (
    _PlanningContextAdapter,  # pyright: ignore[reportPrivateUsage]
    build_model_gateway,
)
from app.modules.planning_runs.adapters.transaction import PostgreSQLPlanningRunTransactionFactory
from app.modules.planning_runs.domain.models import WorkflowRun
from work_management_ai.schemas.progressive_planning import DraftBinding, PlanningDecision
from work_management_ai.workflows.planning.context import (
    PermittedPlanningContext,
    PlanningContextRequest,
)
from work_management_ai.workflows.planning.draft_verifier import DraftValidationResult
from work_management_ai.workflows.planning.graph import PlanningGraph
from work_management_ai.workflows.planning.ports import (
    PersistedProposalReference,
    PlanningCheckpoint,
    PlanningProgressEvent,
    PlanningProposalDraft,
)
from work_management_ai.workflows.planning.state import (
    ProgressivePlanningState,
    create_planning_state,
)


class Context:
    async def load_permitted_context(
        self, request: PlanningContextRequest
    ) -> PermittedPlanningContext:
        return PermittedPlanningContext(("message:1",), (), {"current_message_ref": "message:1"})


class Persistence:
    def __init__(self) -> None:
        self.decisions: list[PlanningDecision] = []

    async def save_checkpoint(self, checkpoint: PlanningCheckpoint) -> None:
        pass

    async def append_progress(self, event: PlanningProgressEvent) -> None:
        pass

    async def persist_proposal(self, draft: PlanningProposalDraft) -> PersistedProposalReference:
        raise AssertionError("draft generation must not persist a business proposal")

    async def publish_progressive_result(
        self,
        state: ProgressivePlanningState,
        decision: PlanningDecision,
        validation: DraftValidationResult,
    ) -> DraftBinding | None:
        self.decisions.append(decision)
        if decision.draft is None:
            return None
        return DraftBinding(draft_id=uuid4(), draft_version=1, fingerprint="a" * 64)


class LegacyPersistence(Persistence):
    def __init__(self) -> None:
        super().__init__()
        self.proposals: list[PlanningProposalDraft] = []

    async def persist_proposal(self, draft: PlanningProposalDraft) -> PersistedProposalReference:
        self.proposals.append(draft)
        return PersistedProposalReference(proposal_id=uuid4(), version=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "locale,message,missing_goal",
    [
        ("vi", "Tạo plan cho tôi", True),
        ("vi", "  TẠO KẾ HOẠCH CHO TÔI!!!  ", True),
        ("vi", "Lập plan", True),
        ("vi", "lập kế hoạch", True),
        ("en", "Create a plan for me", True),
        ("en", "Make a plan", True),
        ("vi", "Tổ chức hội thảo", False),
        ("en", "Organize a workshop", False),
        ("vi", "Tạo kế hoạch tuyển dụng", False),
        ("en", "Create a plan for recruitment", False),
        ("vi", "Ra mắt khóa học online cho 100 học viên", False),
        ("en", "Launch an online course for 100 learners", False),
    ],
)
async def test_planning_requires_a_real_goal_before_creating_a_draft(
    locale: Literal["vi", "en"], message: str, missing_goal: bool
) -> None:
    persistence = Persistence()
    graph = PlanningGraph(
        model_gateway=build_model_gateway(Settings(environment="test", ai_provider="mock")),
        context_port=Context(),
        persistence_port=persistence,
        progressive_persistence_port=persistence,
    )
    result = await graph.run_progressive(
        ProgressivePlanningState(
            run_id=uuid4(),
            organization_id=uuid4(),
            actor_membership_id=uuid4(),
            actor_role="MANAGER",
            locale=locale,
            user_brief=message,
        )
    )
    assert result.safe_error_code is None
    assert result.decision is not None
    if missing_goal:
        assert result.decision.kind == "ASK_GOAL"
        assert result.decision.draft is None and result.binding is None
        assert result.decision.question is not None and result.decision.question.blocking
        assert ("mục tiêu" if locale == "vi" else "goal") in result.decision.question.text
        assert ("Thiếu" if locale == "vi" else "Missing") in result.decision.question.text
        return
    assert result.binding is not None and result.validation.can_prepare
    assert result.decision is not None and result.decision.kind == "DRAFT"
    assert result.decision.question is None
    draft = result.decision.draft
    assert draft is not None and draft.tasks and draft.phases and not draft.open_questions
    assert len(persistence.decisions) == 1
    assert draft.brief.objective.status == "USER_STATED"
    assert draft.brief.objective.source_refs == ("message:1",)
    if "hội thảo" in message or "workshop" in message:
        assert "hội thảo" in str(draft.brief.objective.value) or "workshop" in str(
            draft.brief.objective.value
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Tổ chức hội thảo",
        "Plan conference",
        "Tuyển dụng",
        "Organize a workshop",
    ],
)
async def test_legacy_short_request_produces_proposal_without_clarification(message: str) -> None:
    run = WorkflowRun.create(
        organization_id=uuid4(),
        project_id=None,
        requested_by_membership_id=uuid4(),
        workflow_name="project_planning",
        workflow_version="2.0.0",
        verifier_version="1.0.0",
        input_goal_text=message,
    )
    engine = create_database_engine(Settings(environment="test"))
    try:
        adapter = _PlanningContextAdapter(
            PostgreSQLPlanningRunTransactionFactory(create_session_factory(engine)), run
        )
        context = await adapter.load_permitted_context(
            PlanningContextRequest(
                run.id, run.organization_id, run.requested_by_membership_id, "vi", message, ()
            )
        )
        assert context.required_questions == ()
        persistence = LegacyPersistence()
        graph = PlanningGraph(
            model_gateway=build_model_gateway(Settings(environment="test", ai_provider="mock")),
            context_port=adapter,
            persistence_port=persistence,
        )
        result = await graph.run(
            create_planning_state(
                run_id=run.id,
                organization_id=run.organization_id,
                actor_membership_id=run.requested_by_membership_id,
                actor_role="MANAGER",
                locale="vi",
                user_brief=message,
            )
        )
        assert result.state["stage"] == "WAITING_FOR_DECISION"
        assert result.interrupt is not None and result.interrupt.kind == "MANAGER_DECISION_REQUIRED"
        assert not result.state["pending_questions"]
        assert len(persistence.proposals) == 1
        assert all(
            task.assignee_membership_id is None for task in persistence.proposals[0].content.tasks
        )
    finally:
        await engine.dispose()
