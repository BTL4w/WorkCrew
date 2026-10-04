"""Provider-neutral contracts shared by all activated Agent Harnesses."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentId(StrEnum):
    ORCHESTRATOR = "orchestrator"
    WORK_INTELLIGENCE = "work_intelligence"
    PLANNING = "planning"
    ASSIGNMENT = "assignment"
    DAILY_UPDATE = "daily_update"
    RISK = "risk"
    REPORTING = "reporting"


class AgentRunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    AWAITING_INPUT = "AWAITING_INPUT"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RiskLevel(StrEnum):
    READ_ONLY = "READ_ONLY"
    PROPOSAL_ONLY = "PROPOSAL_ONLY"
    EXPLICIT_WRITE = "EXPLICIT_WRITE"


class AgentBudget(_StrictFrozenModel):
    max_model_attempts: int = Field(default=3, ge=0, le=3)
    max_input_tokens: int = Field(default=24000, ge=0, le=48000)
    max_output_tokens: int = Field(default=4000, ge=0, le=8000)
    max_iterations: int = Field(ge=1, le=16)
    max_tool_calls: int = Field(ge=0, le=32)
    max_handoffs: int = Field(default=0, ge=0, le=16)
    max_replans: int = Field(default=0, ge=0, le=4)
    timeout_seconds: int = Field(ge=1, le=180)


class ActorReference(_StrictFrozenModel):
    membership_id: UUID
    organization_id: UUID


class ResolvedActorContext(_StrictFrozenModel):
    membership_id: UUID
    organization_id: UUID
    role: Literal["ADMIN", "MANAGER", "EMPLOYEE"]
    is_active: bool


class ContextReference(_StrictFrozenModel):
    reference_id: UUID
    organization_id: UUID
    resource_type: str = Field(min_length=1, max_length=100)
    resource_id: UUID
    version: int | None = Field(default=None, ge=1)
    fingerprint: str | None = Field(default=None, min_length=1, max_length=128)
    observed_at: datetime
    freshness_required: bool = True


class AgentHandoff(_StrictFrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    orchestration_run_id: UUID
    parent_agent_run_id: UUID
    target_agent_id: AgentId
    target_agent_version: str = Field(min_length=1, max_length=64)
    capability: str = Field(min_length=1, max_length=100)
    objective: str = Field(min_length=1, max_length=4_000)
    typed_input: dict[str, JsonValue]
    context_references: tuple[ContextReference, ...]
    actor: ActorReference
    budget: AgentBudget
    step_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=255)


class RequestedHandoff(_StrictFrozenModel):
    target_capability: str = Field(min_length=1, max_length=100)
    objective: str = Field(min_length=1, max_length=4_000)
    typed_input: dict[str, JsonValue]


class ProposedAction(_StrictFrozenModel):
    action_type: str = Field(min_length=1, max_length=100)
    risk: RiskLevel
    requires_human_gate: bool
    reference_id: UUID | None = None


class VerifierResult(_StrictFrozenModel):
    verifier_id: str = Field(min_length=1, max_length=100)
    verifier_version: str = Field(min_length=1, max_length=64)
    passed: bool
    safe_codes: tuple[str, ...] = ()


class AgentResult(_StrictFrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    agent_id: AgentId
    agent_version: str = Field(min_length=1, max_length=64)
    status: AgentRunStatus
    typed_output: dict[str, JsonValue]
    evidence: tuple[ContextReference, ...] = ()
    proposed_actions: tuple[ProposedAction, ...] = ()
    verifier_results: tuple[VerifierResult, ...] = ()
    requested_handoff: RequestedHandoff | None = None
    model_attempts_used: int = Field(default=0, ge=0, le=3)
    iterations_used: int = Field(default=0, ge=0, le=16)
    tool_calls_used: int = Field(default=0, ge=0, le=32)
    stop_reason: str = Field(min_length=1, max_length=100)
    safe_error_code: str | None = Field(default=None, min_length=1, max_length=100)


class AgentHarness(Protocol):
    async def run(self, handoff: AgentHandoff) -> AgentResult: ...


class ToolExecutionRequest(_StrictFrozenModel):
    agent_run_id: UUID
    tool_id: str = Field(min_length=1, max_length=100)
    tool_version: str = Field(min_length=1, max_length=64)
    call_id: str = Field(min_length=1, max_length=128)
    actor: ActorReference
    typed_input: dict[str, JsonValue]
    idempotency_key: str = Field(min_length=1, max_length=255)


class ToolExecutionResult(_StrictFrozenModel):
    status: Literal["SUCCEEDED", "REJECTED", "FAILED"]
    typed_output: dict[str, JsonValue]
    evidence: tuple[ContextReference, ...] = ()
    safe_error_code: str | None = Field(default=None, min_length=1, max_length=100)


class ToolExecutorPort(Protocol):
    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult: ...


class PublicEvidenceReference(_StrictFrozenModel):
    evidence_id: str = Field(min_length=1, max_length=128)
    resource_type: str = Field(min_length=1, max_length=100)
    resource_id: UUID
    version: int | None = Field(default=None, ge=1)


class TextResponseBlock(_StrictFrozenModel):
    kind: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=20_000)


class ActivityResponseBlock(_StrictFrozenModel):
    kind: Literal["activity"] = "activity"
    label_key: str = Field(min_length=1, max_length=200)
    status: Literal["PENDING", "RUNNING", "COMPLETED", "FAILED"]
    agent_id: AgentId | None = None


class WorkEvidenceResponseBlock(_StrictFrozenModel):
    kind: Literal["work_evidence"] = "work_evidence"
    summary: str = Field(min_length=1, max_length=20_000)
    evidence: tuple[PublicEvidenceReference, ...]


class QuestionResponseBlock(_StrictFrozenModel):
    kind: Literal["question"] = "question"
    question: str = Field(min_length=1, max_length=4_000)
    response_context: dict[str, JsonValue]


class CapabilityUnavailableResponseBlock(_StrictFrozenModel):
    kind: Literal["capability_unavailable"] = "capability_unavailable"
    capability: str = Field(min_length=1, max_length=100)
    message_key: str = Field(min_length=1, max_length=200)


class PlanningRunResponseBlock(_StrictFrozenModel):
    kind: Literal["planning_run"] = "planning_run"
    workflow_run_id: UUID
    status: str = Field(min_length=1, max_length=100)


class ProposalResponseBlock(_StrictFrozenModel):
    kind: Literal["proposal"] = "proposal"
    workflow_run_id: UUID
    proposal_id: UUID
    proposal_version: int = Field(ge=1)
    approval_id: UUID | None


class TeamRecommendationResponseBlock(_StrictFrozenModel):
    kind: Literal["team_recommendation"] = "team_recommendation"
    project_id: UUID
    recommendation_id: UUID
    recommendation_version: int = Field(ge=1)
    status: Literal["PROPOSED", "APPROVED", "REJECTED", "STALE"]
    explanation_status: Literal["NOT_REQUESTED", "AVAILABLE", "UNAVAILABLE"]


class TeamDecisionResultResponseBlock(_StrictFrozenModel):
    kind: Literal["team_decision_result"] = "team_decision_result"
    recommendation_id: UUID
    recommendation_version: int = Field(ge=1)
    decision: Literal["APPROVE", "REJECT"]


class AssignmentResultResponseBlock(_StrictFrozenModel):
    kind: Literal["assignment_result"] = "assignment_result"
    task_id: UUID
    task_version: int = Field(ge=1)
    membership_id: UUID
    warning_codes: tuple[str, ...] = ()


class DailySummaryResponseBlock(_StrictFrozenModel):
    kind: Literal["daily_summary"] = "daily_summary"
    project_id: UUID
    draft_id: UUID | None = None
    expected_version: int = Field(ge=0)
    operation: Literal["CONFIGURE", "PAUSE", "RESUME"]
    needs_manager_confirmation: Literal[True] = True


class DailyUpdateResponseBlock(_StrictFrozenModel):
    kind: Literal["daily_update"] = "daily_update"
    draft_id: UUID
    draft_version: int = Field(ge=1)
    task_id: UUID
    task_version: int = Field(ge=1)
    assessment_id: UUID | None = None
    needs_owner_confirmation: Literal[True] = True


class RiskResponseBlock(_StrictFrozenModel):
    kind: Literal["risk"] = "risk"
    task_id: UUID
    fingerprint: str
    content: dict[str, JsonValue]


class SafeErrorResponseBlock(_StrictFrozenModel):
    kind: Literal["safe_error"] = "safe_error"
    code: str = Field(min_length=1, max_length=100)
    message_key: str = Field(min_length=1, max_length=200)
    manual_fallback: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        exclude_if=lambda value: value is None,
    )


type ResponseBlock = Annotated[
    TextResponseBlock
    | ActivityResponseBlock
    | WorkEvidenceResponseBlock
    | QuestionResponseBlock
    | CapabilityUnavailableResponseBlock
    | PlanningRunResponseBlock
    | ProposalResponseBlock
    | TeamRecommendationResponseBlock
    | TeamDecisionResultResponseBlock
    | AssignmentResultResponseBlock
    | DailySummaryResponseBlock
    | DailyUpdateResponseBlock
    | RiskResponseBlock
    | SafeErrorResponseBlock,
    Field(discriminator="kind"),
]
