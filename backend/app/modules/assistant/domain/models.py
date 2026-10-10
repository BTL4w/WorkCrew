"""Pure domain records and lifecycle guards for durable Assistant execution."""

import json
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, cast
from uuid import UUID, uuid4

_SAFE_ERROR = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
_MAX_MESSAGE_TEXT = 8_000
_MAX_EVENT_BYTES = 64 * 1024
_MAX_PLAN_STEPS = 8


class AssistantDomainError(ValueError):
    """Stable domain validation failure."""


class InvalidAssistantTransitionError(AssistantDomainError):
    """A terminal or unsupported lifecycle edge was requested."""


class AssistantIdempotencyKeyReusedError(AssistantDomainError):
    """One mutation identity was reused for a different fingerprint."""


class ConversationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class MessageRole(StrEnum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"
    SYSTEM = "SYSTEM"


class AssistantTurnStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    AWAITING_INPUT = "AWAITING_INPUT"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED}


class OrchestrationRunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    AWAITING_INPUT = "AWAITING_INPUT"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED}


class AgentRunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    AWAITING_INPUT = "AWAITING_INPUT"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.CANCELLED}


class InvocationStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self is not self.RUNNING


class AssistantJobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED}


def _now() -> datetime:
    return datetime.now(UTC)


def _safe_error(value: str | None) -> str | None:
    if value is not None and _SAFE_ERROR.fullmatch(value) is None:
        raise AssistantDomainError("SAFE_ERROR_CODE_INVALID")
    return value


def _json_bytes(value: object) -> int:
    try:
        return len(
            json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        )
    except (TypeError, ValueError) as error:
        raise AssistantDomainError("JSON_PAYLOAD_INVALID") from error


def _message_text_size(blocks: tuple[dict[str, Any], ...]) -> int:
    return sum(len(str(block.get("text", ""))) for block in blocks if block.get("kind") == "text")


@dataclass(frozen=True, slots=True)
class AssistantConversation:
    id: UUID
    organization_id: UUID
    owner_membership_id: UUID
    locale: Literal["vi", "en"]
    title: str | None
    status: ConversationStatus
    version: int
    last_message_sequence: int
    last_event_sequence: int
    created_at: datetime
    updated_at: datetime
    is_pinned: bool = False

    @classmethod
    def create(
        cls,
        *,
        organization_id: UUID,
        owner_membership_id: UUID,
        locale: Literal["vi", "en"],
        title: str | None = None,
        id: UUID | None = None,
        now: datetime | None = None,
    ) -> "AssistantConversation":
        current = now or _now()
        normalized_title = title.strip() if title is not None else None
        if normalized_title is not None and not 1 <= len(normalized_title) <= 200:
            raise AssistantDomainError("CONVERSATION_TITLE_INVALID")
        return cls(
            id=id or uuid4(),
            organization_id=organization_id,
            owner_membership_id=owner_membership_id,
            locale=locale,
            title=normalized_title,
            status=ConversationStatus.ACTIVE,
            version=1,
            last_message_sequence=0,
            last_event_sequence=0,
            created_at=current,
            updated_at=current,
        )

    def record_message(self, sequence: int, now: datetime | None = None) -> "AssistantConversation":
        if (
            self.status is not ConversationStatus.ACTIVE
            or sequence != self.last_message_sequence + 1
        ):
            raise AssistantDomainError("MESSAGE_SEQUENCE_INVALID")
        return replace(
            self,
            last_message_sequence=sequence,
            version=self.version + 1,
            updated_at=now or _now(),
        )

    def record_event(self, sequence: int, now: datetime | None = None) -> "AssistantConversation":
        if self.status is not ConversationStatus.ACTIVE or sequence != self.last_event_sequence + 1:
            raise AssistantDomainError("EVENT_SEQUENCE_INVALID")
        return replace(
            self,
            last_event_sequence=sequence,
            version=self.version + 1,
            updated_at=now or _now(),
        )

    def archive(self, now: datetime | None = None) -> "AssistantConversation":
        if self.status is not ConversationStatus.ACTIVE:
            raise InvalidAssistantTransitionError("CONVERSATION_ALREADY_ARCHIVED")
        return replace(
            self,
            status=ConversationStatus.ARCHIVED,
            version=self.version + 1,
            updated_at=now or _now(),
        )


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    id: UUID
    organization_id: UUID
    conversation_id: UUID
    sequence: int
    role: MessageRole
    content_blocks: tuple[dict[str, Any], ...]
    created_by_membership_id: UUID | None = None
    turn_id: UUID | None = None
    dedupe_key: str | None = None
    created_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise AssistantDomainError("MESSAGE_SEQUENCE_INVALID")
        if not self.content_blocks or _message_text_size(self.content_blocks) > _MAX_MESSAGE_TEXT:
            raise AssistantDomainError("MESSAGE_TEXT_LIMIT_EXCEEDED")
        if _json_bytes(self.content_blocks) > _MAX_EVENT_BYTES:
            raise AssistantDomainError("MESSAGE_PAYLOAD_LIMIT_EXCEEDED")


@dataclass(frozen=True, slots=True)
class AssistantTurn:
    id: UUID
    organization_id: UUID
    conversation_id: UUID
    user_message_id: UUID
    actor_membership_id: UUID
    objective: str
    locale: Literal["vi", "en"]
    status: AssistantTurnStatus
    safe_error_code: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        organization_id: UUID,
        conversation_id: UUID,
        user_message_id: UUID,
        actor_membership_id: UUID,
        objective: str,
        locale: Literal["vi", "en"],
        id: UUID | None = None,
        now: datetime | None = None,
    ) -> "AssistantTurn":
        current = now or _now()
        normalized = objective.strip()
        if not normalized or len(normalized) > _MAX_MESSAGE_TEXT:
            raise AssistantDomainError("TURN_OBJECTIVE_INVALID")
        return cls(
            id=id or uuid4(),
            organization_id=organization_id,
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            actor_membership_id=actor_membership_id,
            objective=normalized,
            locale=locale,
            status=AssistantTurnStatus.QUEUED,
            safe_error_code=None,
            created_at=current,
            updated_at=current,
        )

    def _move(self, status: AssistantTurnStatus, now: datetime | None = None) -> "AssistantTurn":
        allowed = {
            AssistantTurnStatus.QUEUED: {AssistantTurnStatus.RUNNING, AssistantTurnStatus.FAILED},
            AssistantTurnStatus.RUNNING: {
                AssistantTurnStatus.AWAITING_INPUT,
                AssistantTurnStatus.AWAITING_HUMAN,
                AssistantTurnStatus.COMPLETED,
                AssistantTurnStatus.FAILED,
            },
            AssistantTurnStatus.AWAITING_INPUT: {
                AssistantTurnStatus.RUNNING,
                AssistantTurnStatus.FAILED,
            },
            AssistantTurnStatus.AWAITING_HUMAN: {
                AssistantTurnStatus.RUNNING,
                AssistantTurnStatus.FAILED,
            },
        }
        if status not in allowed.get(self.status, set()):
            raise InvalidAssistantTransitionError(
                f"ASSISTANT_TURN_TRANSITION_INVALID:{self.status}:{status}"
            )
        return replace(self, status=status, updated_at=now or _now())

    def mark_running(self, now: datetime | None = None) -> "AssistantTurn":
        return self._move(AssistantTurnStatus.RUNNING, now)

    def mark_awaiting_input(self, now: datetime | None = None) -> "AssistantTurn":
        return self._move(AssistantTurnStatus.AWAITING_INPUT, now)

    def mark_awaiting_human(self, now: datetime | None = None) -> "AssistantTurn":
        return self._move(AssistantTurnStatus.AWAITING_HUMAN, now)

    def mark_completed(self, now: datetime | None = None) -> "AssistantTurn":
        return self._move(AssistantTurnStatus.COMPLETED, now)

    def mark_failed(self, error_code: str, now: datetime | None = None) -> "AssistantTurn":
        code = _safe_error(error_code)
        return replace(self._move(AssistantTurnStatus.FAILED, now), safe_error_code=code)


@dataclass(frozen=True, slots=True)
class OrchestrationRun:
    id: UUID
    organization_id: UUID
    turn_id: UUID | None
    orchestrator_version: str
    orchestrator_fingerprint: str
    execution_plan: dict[str, Any]
    checkpoint: dict[str, Any]
    budget: dict[str, Any]
    usage: dict[str, Any]
    status: OrchestrationRunStatus
    stop_reason: str | None
    safe_error_code: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime

    trigger_kind: str = "CHAT_TURN"
    actor_membership_id: UUID | None = None
    project_id: UUID | None = None
    report_id: UUID | None = None
    base_version_id: UUID | None = None
    snapshot_id: UUID | None = None
    snapshot_hash: str | None = None
    summary_id: UUID | None = None
    request_key: str | None = None

    @classmethod
    def create(
        cls,
        *,
        organization_id: UUID,
        turn_id: UUID,
        orchestrator_version: str,
        orchestrator_fingerprint: str,
        execution_plan: dict[str, Any],
        budget: dict[str, Any],
        id: UUID | None = None,
        now: datetime | None = None,
    ) -> "OrchestrationRun":
        steps_value: object = execution_plan.get("steps", [])
        if not isinstance(steps_value, list):
            raise AssistantDomainError("ORCHESTRATION_PLAN_LIMIT_EXCEEDED")
        steps = cast(list[object], steps_value)
        if len(steps) > _MAX_PLAN_STEPS:
            raise AssistantDomainError("ORCHESTRATION_PLAN_LIMIT_EXCEEDED")
        current = now or _now()
        return cls(
            id=id or uuid4(),
            organization_id=organization_id,
            turn_id=turn_id,
            orchestrator_version=orchestrator_version,
            orchestrator_fingerprint=orchestrator_fingerprint,
            execution_plan=execution_plan,
            checkpoint={},
            budget=budget,
            usage={},
            status=OrchestrationRunStatus.QUEUED,
            stop_reason=None,
            safe_error_code=None,
            created_at=current,
            started_at=None,
            completed_at=None,
            updated_at=current,
        )

    def mark_running(self, now: datetime | None = None) -> "OrchestrationRun":
        if self.status not in {
            OrchestrationRunStatus.QUEUED,
            OrchestrationRunStatus.AWAITING_INPUT,
            OrchestrationRunStatus.AWAITING_HUMAN,
        }:
            raise InvalidAssistantTransitionError("ORCHESTRATION_TRANSITION_INVALID")
        current = now or _now()
        return replace(
            self,
            status=OrchestrationRunStatus.RUNNING,
            started_at=self.started_at or current,
            updated_at=current,
        )

    def mark_completed(self, stop_reason: str, now: datetime | None = None) -> "OrchestrationRun":
        if self.status is not OrchestrationRunStatus.RUNNING:
            raise InvalidAssistantTransitionError("ORCHESTRATION_TRANSITION_INVALID")
        current = now or _now()
        return replace(
            self,
            status=OrchestrationRunStatus.COMPLETED,
            stop_reason=stop_reason,
            completed_at=current,
            updated_at=current,
        )

    def mark_failed(self, error_code: str, now: datetime | None = None) -> "OrchestrationRun":
        if self.status.is_terminal:
            raise InvalidAssistantTransitionError("ORCHESTRATION_TERMINAL")
        current = now or _now()
        return replace(
            self,
            status=OrchestrationRunStatus.FAILED,
            safe_error_code=_safe_error(error_code),
            completed_at=current,
            updated_at=current,
        )


@dataclass(frozen=True, slots=True)
class AgentRun:
    id: UUID
    organization_id: UUID
    orchestration_run_id: UUID
    parent_agent_run_id: UUID | None
    inbound_handoff_id: UUID | None
    agent_id: str
    agent_version: str
    manifest_fingerprint: str
    capability: str
    typed_input: dict[str, Any]
    typed_output: dict[str, Any] | None
    version_metadata: dict[str, Any]
    budget: dict[str, Any]
    usage: dict[str, Any]
    status: AgentRunStatus
    stop_reason: str | None
    safe_error_code: str | None
    workflow_run_id: UUID | None
    projected_workflow_sequence: int | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        organization_id: UUID,
        orchestration_run_id: UUID,
        agent_id: str,
        agent_version: str,
        manifest_fingerprint: str,
        capability: str,
        typed_input: dict[str, Any],
        budget: dict[str, Any],
        id: UUID | None = None,
        parent_agent_run_id: UUID | None = None,
        inbound_handoff_id: UUID | None = None,
        workflow_run_id: UUID | None = None,
        projected_workflow_sequence: int | None = None,
        version_metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> "AgentRun":
        current = now or _now()
        return cls(
            id=id or uuid4(),
            organization_id=organization_id,
            orchestration_run_id=orchestration_run_id,
            parent_agent_run_id=parent_agent_run_id,
            inbound_handoff_id=inbound_handoff_id,
            agent_id=agent_id,
            agent_version=agent_version,
            manifest_fingerprint=manifest_fingerprint,
            capability=capability,
            typed_input=typed_input,
            typed_output=None,
            version_metadata=version_metadata or {},
            budget=budget,
            usage={},
            status=AgentRunStatus.QUEUED,
            stop_reason=None,
            safe_error_code=None,
            workflow_run_id=workflow_run_id,
            projected_workflow_sequence=projected_workflow_sequence,
            created_at=current,
            started_at=None,
            completed_at=None,
            updated_at=current,
        )

    def mark_running(self, now: datetime | None = None) -> "AgentRun":
        if self.status is not AgentRunStatus.QUEUED:
            raise InvalidAssistantTransitionError("AGENT_RUN_TRANSITION_INVALID")
        current = now or _now()
        return replace(self, status=AgentRunStatus.RUNNING, started_at=current, updated_at=current)

    def resume(self, now: datetime | None = None) -> "AgentRun":
        if self.status not in {AgentRunStatus.AWAITING_INPUT, AgentRunStatus.AWAITING_HUMAN}:
            raise InvalidAssistantTransitionError("AGENT_RUN_TRANSITION_INVALID")
        current = now or _now()
        return replace(
            self,
            status=AgentRunStatus.RUNNING,
            stop_reason=None,
            safe_error_code=None,
            updated_at=current,
        )

    def mark_completed(
        self,
        *,
        typed_output: dict[str, Any],
        stop_reason: str,
        usage: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> "AgentRun":
        if self.status is not AgentRunStatus.RUNNING:
            raise InvalidAssistantTransitionError("AGENT_RUN_TRANSITION_INVALID")
        current = now or _now()
        return replace(
            self,
            status=AgentRunStatus.COMPLETED,
            typed_output=typed_output,
            stop_reason=stop_reason,
            usage=usage or self.usage,
            completed_at=current,
            updated_at=current,
        )

    def mark_awaiting(
        self,
        *,
        status: AgentRunStatus,
        typed_output: dict[str, Any],
        stop_reason: str,
        usage: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> "AgentRun":
        if self.status is not AgentRunStatus.RUNNING or status not in {
            AgentRunStatus.AWAITING_INPUT,
            AgentRunStatus.AWAITING_HUMAN,
        }:
            raise InvalidAssistantTransitionError("AGENT_RUN_TRANSITION_INVALID")
        current = now or _now()
        return replace(
            self,
            status=status,
            typed_output=typed_output,
            stop_reason=stop_reason,
            usage=usage or self.usage,
            updated_at=current,
        )

    def mark_failed(self, error_code: str, now: datetime | None = None) -> "AgentRun":
        if self.status.is_terminal:
            raise InvalidAssistantTransitionError("AGENT_RUN_TERMINAL")
        current = now or _now()
        return replace(
            self,
            status=AgentRunStatus.FAILED,
            safe_error_code=_safe_error(error_code),
            completed_at=current,
            updated_at=current,
        )


@dataclass(frozen=True, slots=True)
class AgentHandoffRecord:
    id: UUID
    organization_id: UUID
    orchestration_run_id: UUID
    parent_agent_run_id: UUID
    target_agent_id: str
    target_agent_version: str
    capability: str
    objective: str
    typed_input: dict[str, Any]
    context_references: tuple[dict[str, Any], ...]
    budget: dict[str, Any]
    step_id: str
    idempotency_key: str
    dedupe_key: str
    created_at: datetime = field(default_factory=_now)


@dataclass(frozen=True, slots=True)
class AgentCheckpoint:
    id: UUID
    organization_id: UUID
    orchestration_run_id: UUID
    agent_run_id: UUID
    sequence: int
    node: str
    typed_state: dict[str, Any]
    checkpoint_version: str
    created_at: datetime = field(default_factory=_now)


@dataclass(frozen=True, slots=True)
class AgentContextReferenceRecord:
    id: UUID
    organization_id: UUID
    agent_run_id: UUID
    resource_type: str
    resource_id: UUID
    resource_version: int | None
    fingerprint: str | None
    permission_scope: str
    freshness_required: bool
    observed_at: datetime
    created_at: datetime = field(default_factory=_now)


@dataclass(frozen=True, slots=True)
class SkillInvocation:
    id: UUID
    organization_id: UUID
    agent_run_id: UUID
    skill_id: str
    skill_version: str
    typed_input: dict[str, Any]
    typed_output: dict[str, Any] | None
    status: InvocationStatus
    safe_error_code: str | None
    dedupe_key: str
    created_at: datetime = field(default_factory=_now)
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    id: UUID
    organization_id: UUID
    agent_run_id: UUID
    tool_id: str
    tool_version: str
    risk_level: str
    typed_input: dict[str, Any]
    typed_output: dict[str, Any] | None
    context_references: tuple[dict[str, Any], ...]
    status: InvocationStatus
    idempotency_key: str
    dedupe_key: str
    safe_error_code: str | None
    created_at: datetime = field(default_factory=_now)
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AgentModelInvocation:
    id: UUID
    organization_id: UUID
    agent_run_id: UUID
    provider: str
    model: str
    prompt_version: str
    schema_version: str
    invocation_key: str
    status: InvocationStatus
    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_ms: int | None = None
    safe_error_code: str | None = None
    created_at: datetime = field(default_factory=_now)


@dataclass(frozen=True, slots=True)
class AssistantEvent:
    id: UUID
    organization_id: UUID
    conversation_id: UUID
    sequence: int
    event_type: str
    public_payload: dict[str, Any]
    turn_id: UUID | None = None
    orchestration_run_id: UUID | None = None
    agent_run_id: UUID | None = None
    source_type: str | None = None
    source_id: UUID | None = None
    source_sequence: int | None = None
    dedupe_key: str | None = None
    occurred_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise AssistantDomainError("EVENT_SEQUENCE_INVALID")
        if _json_bytes(self.public_payload) > _MAX_EVENT_BYTES:
            raise AssistantDomainError("EVENT_PAYLOAD_LIMIT_EXCEEDED")


@dataclass(frozen=True, slots=True)
class AssistantJob:
    id: UUID
    organization_id: UUID
    conversation_id: UUID
    turn_id: UUID
    orchestration_run_id: UUID
    requester_membership_id: UUID
    job_type: Literal["assistant.turn.execute", "assistant.conversation.title"]
    payload: dict[str, Any]
    status: AssistantJobStatus
    attempt_count: int
    max_attempts: int
    available_at: datetime
    locked_by: str | None
    lease_until: datetime | None
    safe_error_code: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        organization_id: UUID,
        conversation_id: UUID,
        turn_id: UUID,
        orchestration_run_id: UUID,
        requester_membership_id: UUID,
        payload: dict[str, Any],
        max_attempts: int = 3,
        available_at: datetime | None = None,
        id: UUID | None = None,
        now: datetime | None = None,
    ) -> "AssistantJob":
        if not 1 <= max_attempts <= 10:
            raise AssistantDomainError("JOB_ATTEMPT_LIMIT_INVALID")
        current = now or _now()
        return cls(
            id=id or uuid4(),
            organization_id=organization_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            orchestration_run_id=orchestration_run_id,
            requester_membership_id=requester_membership_id,
            job_type="assistant.turn.execute",
            payload=payload,
            status=AssistantJobStatus.QUEUED,
            attempt_count=0,
            max_attempts=max_attempts,
            available_at=available_at or current,
            locked_by=None,
            lease_until=None,
            safe_error_code=None,
            created_at=current,
            updated_at=current,
        )

    def claim(self, *, worker_id: str, lease_until: datetime, now: datetime) -> "AssistantJob":
        reclaimable = self.status is AssistantJobStatus.RUNNING and (
            self.lease_until is not None and self.lease_until < now
        )
        if (
            (self.status is not AssistantJobStatus.QUEUED and not reclaimable)
            or self.available_at > now
            or self.attempt_count >= self.max_attempts
        ):
            raise InvalidAssistantTransitionError("ASSISTANT_JOB_NOT_CLAIMABLE")
        return replace(
            self,
            status=AssistantJobStatus.RUNNING,
            attempt_count=self.attempt_count + 1,
            locked_by=worker_id,
            lease_until=lease_until,
            updated_at=now,
        )

    def complete(self, *, worker_id: str, now: datetime) -> "AssistantJob":
        if self.status is not AssistantJobStatus.RUNNING or self.locked_by != worker_id:
            raise InvalidAssistantTransitionError("ASSISTANT_JOB_LEASE_INVALID")
        return replace(
            self,
            status=AssistantJobStatus.COMPLETED,
            locked_by=None,
            lease_until=None,
            updated_at=now,
        )

    def fail(
        self,
        *,
        worker_id: str,
        error_code: str,
        next_available_at: datetime,
        now: datetime,
    ) -> "AssistantJob":
        if self.status is not AssistantJobStatus.RUNNING or self.locked_by != worker_id:
            raise InvalidAssistantTransitionError("ASSISTANT_JOB_LEASE_INVALID")
        status = (
            AssistantJobStatus.QUEUED
            if self.attempt_count < self.max_attempts
            else AssistantJobStatus.FAILED
        )
        return replace(
            self,
            status=status,
            available_at=next_available_at,
            locked_by=None,
            lease_until=None,
            safe_error_code=_safe_error(error_code),
            updated_at=now,
        )


class AssistantJobClaimLost(RuntimeError):
    """Discard a worker continuation whose fenced claim no longer belongs to it."""
