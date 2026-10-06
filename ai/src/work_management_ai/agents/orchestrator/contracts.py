"""Typed contracts for the Phase 2 Orchestrator Agent."""

from datetime import date
from enum import StrEnum
from typing import Literal, Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from work_management_ai.agents.daily_update.contracts import DailyUpdateHandoff
from work_management_ai.runtime.contracts import (
    ActorReference,
    AgentHandoff,
    AgentId,
    AgentResult,
    JsonValue,
    ResolvedActorContext,
    ResponseBlock,
)
from work_management_ai.runtime.triggers import NonChatTrigger


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReportIntent(_StrictFrozenModel):
    operation: Literal["PREPARE_REPORT", "EXPLAIN_STATUS"]
    project_reference: str = Field(min_length=1, max_length=200)
    kind: Literal["DAILY", "WEEKLY"] = "DAILY"
    period_start: date | None = None
    relative_period: Literal["CURRENT", "PREVIOUS"] = "CURRENT"
    timezone: str | None = Field(default=None, max_length=100)
    locale: Literal["vi", "en"]


class ScheduleIntent(_StrictFrozenModel):
    project_reference: str = Field(min_length=1, max_length=200)
    operation: Literal["CONFIGURE", "PAUSE", "RESUME"]
    timezone: str | None = None
    cutoff: str | None = None
    weekdays: tuple[int, ...] | None = None
    recipient_references: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=100)
    send_when_complete: bool | None = None
    partial_at_cutoff: bool | None = None


class StepMode(StrEnum):
    READ_ONLY = "READ_ONLY"
    PROPOSAL = "PROPOSAL"
    EXPLICIT_WRITE = "EXPLICIT_WRITE"


class ExecutionStep(_StrictFrozenModel):
    step_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    target_agent_id: AgentId
    target_agent_version: str = Field(min_length=1, max_length=64)
    capability: str = Field(min_length=1, max_length=100)
    objective: str = Field(min_length=1, max_length=4_000)
    typed_input: dict[str, JsonValue]
    depends_on: tuple[str, ...] = Field(default=(), max_length=8)
    mode: StepMode


class ExecutionPlan(_StrictFrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    schedule_intent: ScheduleIntent | None = None
    objectives: tuple[str, ...] = Field(min_length=1, max_length=8)
    steps: tuple[ExecutionStep, ...] = Field(default=(), max_length=8)
    unavailable_capabilities: tuple[str, ...] = Field(default=(), max_length=8)
    response_language: Literal["vi", "en"]

    @model_validator(mode="after")
    def require_an_executable_or_unavailable_outcome(self) -> "ExecutionPlan":
        if self.schedule_intent is not None and self.steps:
            raise ValueError("schedule preview cannot be combined with specialist writes")
        if not self.steps and not self.unavailable_capabilities and self.schedule_intent is None:
            raise ValueError("execution plan must contain a step or unavailable capability")
        return self


class ConversationExcerpt(_StrictFrozenModel):
    role: Literal["USER", "ASSISTANT"]
    text: str = Field(min_length=1, max_length=8_000)


class ActivePlanningContext(_StrictFrozenModel):
    workflow_run_id: UUID
    workflow_status: str = Field(min_length=1, max_length=100)
    proposal_id: UUID | None
    proposal_version: int | None = Field(default=None, ge=1)
    proposal_status: str | None = Field(default=None, min_length=1, max_length=100)
    requested_operation: Literal["RESUME_INPUT", "REVISE"] | None = None


class ActiveTeamContext(_StrictFrozenModel):
    project_id: UUID
    planning_proposal_id: UUID | None = None
    planning_proposal_version: int | None = Field(default=None, ge=1)
    recommendation_id: UUID | None = None
    recommendation_version: int | None = Field(default=None, ge=1)
    recommendation_status: str | None = Field(default=None, min_length=1, max_length=100)
    requested_operation: Literal["REVISE_TEAM", "RECOMMEND_TEAM", "ANALYZE_WORKLOAD"] | None = None

    @model_validator(mode="after")
    def planning_provenance_is_complete(self) -> "ActiveTeamContext":
        if (self.planning_proposal_id is None) != (self.planning_proposal_version is None):
            raise ValueError("planning proposal provenance must be complete")
        return self


class ExactAssignmentContext(_StrictFrozenModel):
    """Authoritative result of tenant-filtered Task/person resolution."""

    project_id: UUID
    task_id: UUID
    task_version: int = Field(ge=1)
    membership_id: UUID


class ExactAssignmentResolution(_StrictFrozenModel):
    exact_context: ExactAssignmentContext | None = None
    team_context: ActiveTeamContext | None = None
    issue: (
        Literal[
            "TASK_AMBIGUOUS_OR_NOT_FOUND",
            "MEMBER_AMBIGUOUS_OR_NOT_FOUND",
            "PROJECT_AMBIGUOUS_OR_NOT_FOUND",
            "ASSIGNMENT_FORBIDDEN",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def exact_or_clarification_but_not_both(self) -> "ExactAssignmentResolution":
        populated = sum(
            value is not None for value in (self.exact_context, self.team_context, self.issue)
        )
        if populated > 1:
            raise ValueError("resolved assignment contexts are mutually exclusive")
        return self


class PendingFollowup(_StrictFrozenModel):
    kind: Literal["RECOMMEND_PROJECT_TEAM"] = "RECOMMEND_PROJECT_TEAM"
    planning_workflow_run_id: UUID
    planning_proposal_id: UUID | None = None
    planning_proposal_version: int | None = Field(default=None, ge=1)
    project_id: UUID | None = None
    requirement_set_id: UUID | None = None
    requirement_version: int | None = Field(default=None, ge=1)
    state: Literal[
        "WAITING_PROJECT_PROPOSAL",
        "WAITING_PROJECT_DECISION",
        "WAITING_TEAM_REQUIREMENTS",
        "READY",
        "COMPLETED",
        "CANCELLED",
    ]

    @model_validator(mode="after")
    def proposal_is_bound_after_generation(self) -> "PendingFollowup":
        if self.state == "WAITING_PROJECT_PROPOSAL":
            if self.planning_proposal_id is not None or self.planning_proposal_version is not None:
                raise ValueError("proposal must not be bound before generation")
        elif self.planning_proposal_id is None or self.planning_proposal_version is None:
            raise ValueError("proposal identity is required after generation")
        if self.state == "WAITING_TEAM_REQUIREMENTS" and (
            self.project_id is None
            or self.requirement_set_id is None
            or self.requirement_version is None
        ):
            raise ValueError("team requirement identity is required while waiting")
        return self

    def bind_proposal(self, *, proposal_id: UUID, proposal_version: int) -> Self:
        if self.state != "WAITING_PROJECT_PROPOSAL":
            raise ValueError("FOLLOWUP_STATE_INVALID")
        return self.model_copy(
            update={
                "planning_proposal_id": proposal_id,
                "planning_proposal_version": proposal_version,
                "state": "WAITING_PROJECT_DECISION",
            }
        )

    def mark_ready(self, *, project_id: UUID | None = None) -> Self:
        if self.state not in {"WAITING_PROJECT_DECISION", "WAITING_TEAM_REQUIREMENTS"}:
            raise ValueError("FOLLOWUP_STATE_INVALID")
        if self.state == "WAITING_PROJECT_DECISION" and project_id is None:
            raise ValueError("PROJECT_CONTEXT_REQUIRED")
        updates: dict[str, object] = {"state": "READY"}
        if project_id is not None:
            updates["project_id"] = project_id
        return self.model_copy(update=updates)

    def wait_for_requirements(
        self,
        *,
        project_id: UUID,
        requirement_set_id: UUID,
        requirement_version: int,
    ) -> Self:
        if self.state != "READY":
            raise ValueError("FOLLOWUP_STATE_INVALID")
        return self.model_copy(
            update={
                "project_id": project_id,
                "requirement_set_id": requirement_set_id,
                "requirement_version": requirement_version,
                "state": "WAITING_TEAM_REQUIREMENTS",
            }
        )

    def mark_cancelled(self) -> Self:
        if self.state != "WAITING_PROJECT_DECISION":
            raise ValueError("FOLLOWUP_STATE_INVALID")
        return self.model_copy(update={"state": "CANCELLED"})

    def mark_completed(self) -> Self:
        if self.state != "READY":
            raise ValueError("FOLLOWUP_STATE_INVALID")
        return self.model_copy(update={"state": "COMPLETED"})


class ActiveConversationContext(_StrictFrozenModel):
    daily_update: DailyUpdateHandoff | None = None
    daily_update_resolution_issue: bool = False
    recent_messages: tuple[ConversationExcerpt, ...] = Field(max_length=12)
    active_planning: ActivePlanningContext | None = None
    active_team: ActiveTeamContext | None = None
    exact_assignment: ExactAssignmentContext | None = None
    assignment_resolution_issue: (
        Literal[
            "TASK_AMBIGUOUS_OR_NOT_FOUND",
            "MEMBER_AMBIGUOUS_OR_NOT_FOUND",
            "PROJECT_AMBIGUOUS_OR_NOT_FOUND",
            "ASSIGNMENT_FORBIDDEN",
        ]
        | None
    ) = None


class OrchestratorInput(_StrictFrozenModel):
    orchestration_run_id: UUID | None = None
    conversation_id: UUID
    turn_id: UUID
    message: str = Field(min_length=1, max_length=8_000)
    locale: Literal["vi", "en"]
    actor: ActorReference
    active_context: ActiveConversationContext


class OrchestratorSynthesis(_StrictFrozenModel):
    blocks: tuple[ResponseBlock, ...] = Field(min_length=1, max_length=16)


class OrchestratorStatus(StrEnum):
    COMPLETED = "COMPLETED"
    AWAITING_INPUT = "AWAITING_INPUT"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    FAILED = "FAILED"


class OrchestratorOutput(_StrictFrozenModel):
    execution_plan: ExecutionPlan | None
    agent_results: tuple[AgentResult, ...]
    blocks: tuple[ResponseBlock, ...]
    completed_step_ids: tuple[str, ...]
    status: OrchestratorStatus
    stop_reason: str = Field(min_length=1, max_length=100)
    replans_used: int = Field(ge=0, le=2)
    model_refs: tuple[str, ...]
    pending_followup: PendingFollowup | None = None


class ActorContextResolverPort(Protocol):
    async def resolve(self, reference: ActorReference) -> ResolvedActorContext: ...


class SpecialistRunnerPort(Protocol):
    async def run_specialist(self, handoff: AgentHandoff) -> AgentResult: ...


class OrchestratorTriggerInput(_StrictFrozenModel):
    orchestration_run_id: UUID
    actor: ActorReference
    locale: Literal["vi", "en"]
    trigger: NonChatTrigger
