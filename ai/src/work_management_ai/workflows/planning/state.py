"""Typed, resumable state for the bounded planning graph."""

from collections.abc import Iterable
from dataclasses import asdict, dataclass, is_dataclass
from typing import Literal, TypedDict, cast
from uuid import UUID

from pydantic import BaseModel

from work_management_ai.prompts.planning import PLANNING_PROMPT_VERSION
from work_management_ai.schemas.planning import PlanningModelOutput, ProposedTask
from work_management_ai.schemas.progressive_planning import (
    DraftBinding,
    PlanningDecision,
    PlanningDraft,
    PlanningQuestion,
)
from work_management_ai.workflows.planning.draft_verifier import DraftValidationResult
from work_management_ai.workflows.planning.verifier import (
    PLANNING_VERIFIER_VERSION,
    PlanningValidationResult,
)

PLANNING_WORKFLOW_VERSION = "2.0.0"
PLANNING_SCHEMA_VERSION = "planning-proposal.v2"

PlanningLocale = Literal["vi", "en"]
PlanningActorRole = Literal["ADMIN", "MANAGER", "EMPLOYEE"]


class PlanningState(TypedDict):
    """Structured execution/audit state; never contains prompts or hidden reasoning."""

    run_id: UUID
    organization_id: UUID
    actor_membership_id: UUID
    actor_role: PlanningActorRole
    locale: PlanningLocale
    stage: str
    user_brief: str
    context_reference_ids: tuple[str, ...]
    understanding: str | None
    assumptions: tuple[str, ...]
    manager_answers: tuple[str, ...]
    pending_questions: tuple[str, ...]
    proposal: PlanningModelOutput | None
    proposal_id: UUID | None
    proposal_version: int | None
    validation_result: PlanningValidationResult | None
    schema_error_code: str | None
    schema_repair_count: int
    verifier_revision_count: int
    workflow_version: str
    prompt_version: str
    schema_version: str
    model_reference: str | None
    verifier_version: str


def create_planning_state(
    *,
    run_id: UUID,
    organization_id: UUID,
    actor_membership_id: UUID,
    actor_role: PlanningActorRole,
    locale: PlanningLocale,
    user_brief: str,
) -> PlanningState:
    """Create a complete initial state with stable version metadata."""

    return PlanningState(
        run_id=run_id,
        organization_id=organization_id,
        actor_membership_id=actor_membership_id,
        actor_role=actor_role,
        locale=locale,
        stage="RECEIVED",
        user_brief=user_brief,
        context_reference_ids=(),
        understanding=None,
        assumptions=(),
        manager_answers=(),
        pending_questions=(),
        proposal=None,
        proposal_id=None,
        proposal_version=None,
        validation_result=None,
        schema_error_code=None,
        schema_repair_count=0,
        verifier_revision_count=0,
        workflow_version=PLANNING_WORKFLOW_VERSION,
        prompt_version=PLANNING_PROMPT_VERSION,
        schema_version=PLANNING_SCHEMA_VERSION,
        model_reference=None,
        verifier_version=PLANNING_VERIFIER_VERSION,
    )


def checkpoint_state(state: PlanningState) -> dict[str, object]:
    """Return a JSON-safe allowlisted checkpoint without transient model context."""

    return {key: _json_value(value) for key, value in state.items()}


class PlanningRevisionError(ValueError):
    pass


def merge_revision_assignees(
    base: PlanningModelOutput,
    draft: PlanningModelOutput,
) -> PlanningModelOutput:
    """Keep all proposed work unassigned; assignment is a later Manager action."""

    _unique_tasks(base, code="BASE_TASK_REF_INVALID")
    _unique_tasks(draft, code="REVISION_TASK_REF_INVALID")
    tasks = [task.model_copy(update={"assignee_membership_id": None}) for task in draft.tasks]
    return draft.model_copy(update={"tasks": tasks})


def _unique_tasks(
    plan: PlanningModelOutput,
    *,
    code: str,
) -> dict[str, ProposedTask]:
    by_ref: dict[str, ProposedTask] = {}
    for task in plan.tasks:
        if not task.ref.strip() or task.ref in by_ref:
            raise PlanningRevisionError(code)
        by_ref[task.ref] = task
    return by_ref


def _json_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, BaseModel):
        return cast(object, value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(cast(dict[str, object], asdict(value)))
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        return {str(key): _json_value(item) for key, item in mapping.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in cast(Iterable[object], value)]
    return value


@dataclass(frozen=True, slots=True)
class ProgressivePlanningState:
    """Versioned operational state for a bounded relative planning turn."""

    run_id: UUID
    organization_id: UUID
    actor_membership_id: UUID
    actor_role: PlanningActorRole
    locale: PlanningLocale
    user_brief: str
    context_reference_ids: tuple[str, ...] = ()
    current_draft: PlanningDraft | None = None
    current_binding: DraftBinding | None = None
    pending_question: PlanningQuestion | None = None
    model_calls_used: int = 0
    max_model_calls: int = 3
    workflow_version: str = "3.0.0"
    schema_version: str = "planning-draft.v1"
    model_reference: str = "unknown"
    prompt_version: str = "progressive-planning.v1"
    verifier_version: str = "1.0.0"


@dataclass(frozen=True, slots=True)
class ProgressivePlanningResult:
    decision: PlanningDecision | None
    validation: DraftValidationResult
    binding: DraftBinding | None
    model_calls_used: int
    safe_error_code: str | None = None
