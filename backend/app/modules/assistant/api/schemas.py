"""Request/response schemas for the Assistant conversation API — Task 6."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from app.modules.assistant.domain.models import AssistantConversation, AssistantMessage
from work_management_ai.agents.risk.contracts import RiskCardContent
from work_management_ai.runtime.contracts import ProjectStatusResponseBlock, ReportResponseBlock

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    locale: Literal["vi", "en"]
    title: str | None = Field(default=None, max_length=120)


class UpdateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str | None = Field(default=None, min_length=1, max_length=120)
    is_pinned: bool | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def validate_changes(self) -> UpdateConversationRequest:
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set
        ):
            raise ValueError("CONVERSATION_CHANGE_REQUIRED")
        return self


class CardAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["PLANNING_INPUT", "PLANNING_REVISE", "TEAM_REVISE"]
    workflow_run_id: UUID | None = None
    proposal_id: UUID | None = None
    recommendation_id: UUID | None = None
    recommendation_version: int | None = Field(default=None, ge=1)

    @classmethod
    def _missing_exact_reference(cls) -> ValueError:
        return ValueError("CARD_ACTION_REFERENCE_MISSING")

    def model_post_init(self, __context: Any) -> None:
        if self.kind == "TEAM_REVISE":
            if self.recommendation_id is None or self.recommendation_version is None:
                raise self._missing_exact_reference()
        elif self.workflow_run_id is None:
            raise self._missing_exact_reference()


class PostAssistantMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=8000)
    locale: Literal["vi", "en"]
    card_action: CardAction | None = None


# ---------------------------------------------------------------------------
# Content block discriminated union (strict)
# ---------------------------------------------------------------------------


class TextBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["text"] = "text"
    text: str


class ActivityBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["activity"] = "activity"
    label_key: str
    status: Literal["PENDING", "RUNNING", "COMPLETED", "FAILED"]
    agent_id: str | None = None
    workflow_run_id: UUID | None = None


class WorkEvidenceBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["work_evidence"] = "work_evidence"
    summary: str
    evidence: list[dict[str, Any]]


class QuestionBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["question"] = "question"
    question: str
    response_context: dict[str, Any] = Field(default_factory=dict)


class CapabilityUnavailableBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["capability_unavailable"] = "capability_unavailable"
    capability: str
    message_key: str


class PlanningRunBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["planning_run"] = "planning_run"
    workflow_run_id: UUID
    status: str


class ProposalBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["proposal"] = "proposal"
    workflow_run_id: UUID
    proposal_id: UUID
    proposal_version: int
    approval_id: UUID | None = None
    state: str | None = None
    can_approve: bool | None = None
    read_only: bool = False
    current_version: int | None = None
    error_codes: list[str] = Field(default_factory=list)
    manual_fallback: str | None = None


class DecisionResultBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["decision_result"] = "decision_result"
    workflow_run_id: UUID
    decision: Literal["APPROVE", "REJECT", "UNKNOWN"]
    proposal_id: UUID
    proposal_version: int
    project_id: UUID | None = None
    continue_team: bool = False


class TeamRecommendationBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["team_recommendation"] = "team_recommendation"
    project_id: UUID
    recommendation_id: UUID
    recommendation_version: int = Field(ge=1)
    status: Literal["PROPOSED", "APPROVED", "REJECTED", "STALE"]
    explanation_status: Literal["NOT_REQUESTED", "AVAILABLE", "UNAVAILABLE"]


class TeamDecisionResultBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["team_decision_result"] = "team_decision_result"
    recommendation_id: UUID
    recommendation_version: int = Field(ge=1)
    decision: Literal["APPROVE", "REJECT"]


class AssignmentResultBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["assignment_result"] = "assignment_result"
    task_id: UUID
    task_version: int = Field(ge=1)
    membership_id: UUID
    warning_codes: list[str] = Field(default_factory=list)


class DailyUpdateBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["daily_update"] = "daily_update"
    draft_id: UUID
    draft_version: int = Field(ge=1)
    task_id: UUID
    task_version: int = Field(ge=1)
    assessment_id: UUID | None = None
    needs_owner_confirmation: Literal[True] = True


class RiskBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["risk"] = "risk"
    task_id: UUID
    fingerprint: str
    content: RiskCardContent


class DailySummaryBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["daily_summary"] = "daily_summary"
    project_id: UUID
    draft_id: UUID | None = None
    expected_version: int = Field(ge=0)
    operation: Literal["CONFIGURE", "PAUSE", "RESUME"]
    needs_manager_confirmation: Literal[True] = True


class SafeErrorBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["safe_error"] = "safe_error"
    code: str
    message_key: str
    manual_fallback: str | None = None


ContentBlock = Annotated[
    TextBlock
    | ActivityBlock
    | WorkEvidenceBlock
    | QuestionBlock
    | CapabilityUnavailableBlock
    | PlanningRunBlock
    | ProposalBlock
    | DecisionResultBlock
    | TeamRecommendationBlock
    | TeamDecisionResultBlock
    | AssignmentResultBlock
    | DailyUpdateBlock
    | RiskBlock
    | DailySummaryBlock
    | ReportResponseBlock
    | ProjectStatusResponseBlock
    | SafeErrorBlock,
    Field(discriminator="kind"),
]
_CONTENT_BLOCKS = TypeAdapter(list[ContentBlock])


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ConversationResponse(BaseModel):
    id: UUID
    locale: Literal["vi", "en"]
    title: str | None
    status: str
    version: int
    is_pinned: bool
    last_message_sequence: int
    last_event_sequence: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, c: AssistantConversation) -> ConversationResponse:
        return cls(
            id=c.id,
            locale=c.locale,
            title=c.title,
            version=c.version,
            is_pinned=c.is_pinned,
            status=c.status.value if hasattr(c.status, "value") else str(c.status),
            last_message_sequence=c.last_message_sequence,
            last_event_sequence=c.last_event_sequence,
            created_at=c.created_at,
            updated_at=c.updated_at,
        )


class ConversationListResponse(BaseModel):
    items: list[ConversationResponse]


class MessageResponse(BaseModel):
    id: UUID
    sequence: int
    role: str
    content_blocks: list[ContentBlock]
    created_at: datetime

    @classmethod
    def from_domain(cls, m: AssistantMessage) -> MessageResponse:
        public_blocks = tuple(
            _public_block(block, dedupe_key=m.dedupe_key)
            for block in m.content_blocks
            if block.get("kind")
            not in {
                "accepted_card_action",
                "PLANNING_INPUT",
                "PLANNING_REVISE",
                "TEAM_REVISE",
            }
        )
        return cls(
            id=m.id,
            sequence=m.sequence,
            role=m.role.value if hasattr(m.role, "value") else str(m.role),
            content_blocks=_CONTENT_BLOCKS.validate_python(public_blocks),
            created_at=m.created_at,
        )


def _public_block(block: dict[str, Any], *, dedupe_key: str | None) -> dict[str, Any]:
    if block.get("kind") != "activity" or block.get("workflow_run_id") is not None:
        return block
    workflow_run_id = _workflow_run_id_from_dedupe_key(dedupe_key)
    if workflow_run_id is None:
        return block
    return {**block, "workflow_run_id": workflow_run_id}


def _workflow_run_id_from_dedupe_key(dedupe_key: str | None) -> UUID | None:
    if dedupe_key is None:
        return None
    parts = dedupe_key.split(":")
    if len(parts) != 3 or parts[0] != "workflow":
        return None
    try:
        return UUID(parts[1])
    except ValueError:
        return None


class TurnResponse(BaseModel):
    id: UUID
    status: str
    locale: Literal["vi", "en"]
    created_at: str


class ConversationSnapshotResponse(BaseModel):
    conversation: ConversationResponse
    messages: list[MessageResponse]


class AssistantTurnAcceptedResponse(BaseModel):
    conversation_id: UUID
    message_id: UUID
    turn_id: UUID
    orchestration_run_id: UUID
    status: Literal["QUEUED"]
