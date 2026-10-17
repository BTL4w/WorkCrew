"""Exercise the actual Planning graph with deterministic provider fixtures."""

import json
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from work_management_ai.model_gateway.errors import ModelTimeoutError
from work_management_ai.model_gateway.mock import MockModelGateway
from work_management_ai.schemas.progressive_planning import DraftBinding, PlanningDecision
from work_management_ai.workflows.planning.context import (
    PermittedPlanningContext,
    PlanningContextRequest,
)
from work_management_ai.workflows.planning.draft_verifier import DraftValidationResult
from work_management_ai.workflows.planning.graph import PlanningGraph
from work_management_ai.workflows.planning.ports import (
    PlanningCheckpoint,
    PlanningProgressEvent,
    PlanningProposalDraft,
)
from work_management_ai.workflows.planning.state import PlanningLocale, ProgressivePlanningState


class Context:
    async def load_permitted_context(
        self, request: PlanningContextRequest
    ) -> PermittedPlanningContext:
        return PermittedPlanningContext(("message:1",), (), {})


class Persistence:
    def __init__(self) -> None:
        self.decisions: list[PlanningDecision] = []

    async def save_checkpoint(self, checkpoint: PlanningCheckpoint) -> None:
        pass

    async def append_progress(self, event: PlanningProgressEvent) -> None:
        pass

    async def persist_proposal(self, draft: PlanningProposalDraft):
        raise AssertionError("progressive planning must not persist a proposal")

    async def publish_progressive_result(
        self,
        state: ProgressivePlanningState,
        decision: PlanningDecision,
        validation: DraftValidationResult,
    ) -> DraftBinding | None:
        self.decisions.append(decision)
        return (
            DraftBinding(draft_id=uuid4(), draft_version=1, fingerprint="a" * 64)
            if decision.draft
            else None
        )


def state(
    locale: str = "en", message: str = "Create a plan for me", used: int = 0
) -> ProgressivePlanningState:
    return ProgressivePlanningState(
        run_id=uuid4(),
        organization_id=uuid4(),
        actor_membership_id=uuid4(),
        actor_role="MANAGER",
        locale=cast(PlanningLocale, locale),
        user_brief=message,
        model_calls_used=used,
    )


def candidate(kind: str, locale: str) -> dict[str, object]:
    if kind == "DRAFT":
        content = json.loads(
            (Path(__file__).parent / "fixtures/progressive_planning/en.json").read_text()
        )
        return {
            "decision": {
                "kind": "DRAFT",
                "draft": content,
                "question": {
                    "id": "date",
                    "purpose": "FOLLOW_UP",
                    "text": "Bạn đã có ngày ra mắt chưa?"
                    if locale == "vi"
                    else "Do you have a launch date?",
                    "blocking": False,
                },
            },
            "change_assessment": None,
        }
    return {
        "decision": {
            "kind": kind,
            "question": {
                "id": "clarify",
                "purpose": "GOAL" if kind == "ASK_GOAL" else "DECISIVE",
                "text": "Bạn muốn lập kế hoạch cho mục tiêu gì?"
                if locale == "vi"
                else "What goal would you like to plan for?",
                "blocking": True,
                "decision_impact": "Venue and equipment differ between in-person and online"
                if kind == "ASK_DECISIVE"
                else None,
            },
        },
        "change_assessment": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["vi", "en"])
@pytest.mark.parametrize("message", ["Launch an online course", "Organize a workshop"])
async def test_progressive_decision_persists_without_business_proposal(
    locale: str, message: str
) -> None:
    gateway = MockModelGateway(
        fixtures={
            f"planning.progressive.{locale}.content": candidate("DRAFT", locale),
            f"planning.progressive.{locale}.grounding": {"passed": True},
        }
    )
    persistence = Persistence()
    graph = PlanningGraph(
        model_gateway=gateway,
        context_port=Context(),
        persistence_port=persistence,
        progressive_persistence_port=persistence,
    )
    result = await graph.run_progressive(state(locale, message))
    assert result.safe_error_code is None
    assert result.decision is not None and result.decision.kind == "DRAFT"
    assert len(persistence.decisions) == 1
    assert result.binding is not None and result.decision.draft is not None
    assert result.decision.draft.duration_days == 42
    assert result.decision.question is None
    assert result.decision.draft.open_questions == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ASK_GOAL", "ASK_DECISIVE"])
async def test_missing_required_information_pauses_without_a_draft(kind: str) -> None:
    gateway = MockModelGateway(
        fixtures={"planning.progressive.en.content": candidate(kind, "en")},
    )
    persistence = Persistence()
    graph = PlanningGraph(
        model_gateway=gateway,
        context_port=Context(),
        persistence_port=persistence,
        progressive_persistence_port=persistence,
    )
    result = await graph.run_progressive(state())
    assert result.safe_error_code is None
    assert result.decision is not None and result.decision.kind == kind
    assert result.decision.draft is None and result.binding is None
    assert result.model_calls_used == 1
    assert len(persistence.decisions) == 1 and persistence.decisions[0].question is not None


@pytest.mark.asyncio
async def test_model_cannot_publish_an_invented_goal_disguised_as_an_assumption() -> None:
    content = candidate("DRAFT", "en")
    content["decision"]["draft"]["brief"]["objective"] = {  # type: ignore[index]
        "value": "Organize a work objective",
        "status": "AI_ASSUMPTION",
        "assumption_reason": "The user supplied no goal",
    }
    persistence = Persistence()
    graph = PlanningGraph(
        model_gateway=MockModelGateway(fixtures={"planning.progressive.en.content": content}),
        context_port=Context(),
        persistence_port=persistence,
        progressive_persistence_port=persistence,
    )
    result = await graph.run_progressive(state())
    assert result.safe_error_code == "PLANNING_VERIFIER_FAILED"
    assert result.decision is None and persistence.decisions == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture,code",
    [
        (ModelTimeoutError("timeout"), "PLANNING_MODEL_FAILED"),
        ({"decision": {"kind": "DRAFT"}}, "PLANNING_OUTPUT_INVALID"),
    ],
)
async def test_progressive_failures_preserve_manual_fallback(fixture: object, code: str) -> None:
    gateway = MockModelGateway(fixtures={"planning.progressive.en.content": fixture})
    persistence = Persistence()
    graph = PlanningGraph(
        model_gateway=gateway,
        context_port=Context(),
        persistence_port=persistence,
        progressive_persistence_port=persistence,
    )
    result = await graph.run_progressive(state())
    assert result.safe_error_code == code
    assert persistence.decisions == []


@pytest.mark.asyncio
async def test_grounding_failure_never_publishes_unsupported_draft() -> None:
    gateway = MockModelGateway(
        fixtures={
            "planning.progressive.en.content": candidate("DRAFT", "en"),
            "planning.progressive.en.grounding": {
                "passed": False,
                "unsupported_claim_paths": ["brief.objective"],
            },
        }
    )
    persistence = Persistence()
    graph = PlanningGraph(
        model_gateway=gateway,
        context_port=Context(),
        persistence_port=persistence,
        progressive_persistence_port=persistence,
    )
    result = await graph.run_progressive(state(used=1))
    assert result.safe_error_code == "PLANNING_GROUNDING_FAILED"
    assert not persistence.decisions


@pytest.mark.asyncio
async def test_exhausted_budget_does_not_reset_on_resume() -> None:
    persistence = Persistence()
    graph = PlanningGraph(
        model_gateway=MockModelGateway(fixtures={}),
        context_port=Context(),
        persistence_port=persistence,
        progressive_persistence_port=persistence,
    )
    result = await graph.run_progressive(state(used=3))
    assert result.safe_error_code == "PLANNING_MODEL_BUDGET_EXHAUSTED"
    assert result.model_calls_used == 3 and not persistence.decisions
