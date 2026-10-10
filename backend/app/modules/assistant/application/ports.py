"""Typed repository and transaction boundaries for durable Assistant execution."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from app.modules.assistant.domain.models import (
    AgentCheckpoint,
    AgentHandoffRecord,
    AgentModelInvocation,
    AgentRun,
    AssistantConversation,
    AssistantEvent,
    AssistantJob,
    AssistantMessage,
    AssistantTurn,
    OrchestrationRun,
    ToolInvocation,
)
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.planning_runs.domain.models import WorkflowEvent


@dataclass(frozen=True, slots=True)
class AssistantConversationMutationResult:
    conversation: AssistantConversation
    replayed: bool


@dataclass(frozen=True, slots=True)
class AssistantTurnMutationResult:
    message: AssistantMessage
    turn: AssistantTurn
    run: OrchestrationRun
    job: AssistantJob
    event: AssistantEvent
    replayed: bool


@dataclass(frozen=True, slots=True)
class AssistantConversationSnapshot:
    conversation: AssistantConversation
    messages: tuple[AssistantMessage, ...]
    turns: tuple[AssistantTurn, ...]
    orchestration_runs: tuple[OrchestrationRun, ...]
    events: tuple[AssistantEvent, ...]


@dataclass(frozen=True, slots=True)
class LinkedWorkflowEvent:
    agent_run: AgentRun
    turn_id: UUID
    conversation_id: UUID
    event: WorkflowEvent


@dataclass(frozen=True, slots=True)
class ConversationTitleInput:
    message: str
    locale: Literal["vi", "en"]


class AssistantRepository(Protocol):
    async def get_conversation_title_input(
        self, *, actor: AuthenticatedActor, job: AssistantJob
    ) -> ConversationTitleInput | None: ...

    async def set_conversation_title(
        self,
        *,
        actor: AuthenticatedActor,
        job: AssistantJob,
        title: str,
        metadata: dict[str, Any],
    ) -> bool: ...

    async def manage_conversation_mutation(
        self,
        *,
        actor: AuthenticatedActor,
        conversation_id: UUID,
        changes: dict[str, object],
        expected_version: int,
        request_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> AssistantConversationMutationResult: ...

    async def create_conversation_mutation(
        self,
        *,
        actor: AuthenticatedActor,
        conversation: AssistantConversation,
        request_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> AssistantConversationMutationResult: ...

    async def submit_message_mutation(
        self,
        *,
        actor: AuthenticatedActor,
        message: AssistantMessage,
        turn: AssistantTurn,
        run: OrchestrationRun,
        job: AssistantJob,
        event: AssistantEvent,
        request_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> AssistantTurnMutationResult: ...

    async def get_conversation_snapshot(
        self, *, actor: AuthenticatedActor, conversation_id: UUID
    ) -> AssistantConversationSnapshot | None: ...

    async def list_conversations(
        self, *, actor: AuthenticatedActor, limit: int
    ) -> list[AssistantConversation]: ...

    async def list_events(
        self, *, actor: AuthenticatedActor, conversation_id: UUID, after_sequence: int
    ) -> list[AssistantEvent]: ...

    async def append_rejected_audit(
        self,
        *,
        actor: AuthenticatedActor,
        action: str,
        resource_type: str,
        resource_id: UUID | None,
        request_id: str,
        reason_code: str,
    ) -> None: ...

    async def claim_job(
        self,
        *,
        organization_id: UUID,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
        job_type: str = "assistant.turn.execute",
    ) -> AssistantJob | None: ...

    async def begin_orchestration(self, *, job: AssistantJob) -> OrchestrationRun: ...

    async def finish_orchestration(
        self,
        *,
        job: AssistantJob,
        status: str,
        stop_reason: str,
        safe_error_code: str | None,
    ) -> None: ...

    async def append_agent_run(self, *, run: AgentRun) -> AgentRun: ...

    async def get_agent_run(self, *, organization_id: UUID, run_id: UUID) -> AgentRun | None: ...
    async def resume_agent_run(self, *, run: AgentRun) -> AgentRun: ...

    async def get_agent_run_turn_context(
        self, *, organization_id: UUID, run_id: UUID
    ) -> tuple[AgentRun, UUID] | None: ...

    async def get_accepted_planning_action(
        self, *, organization_id: UUID, turn_id: UUID
    ) -> dict[str, Any] | None: ...

    async def link_agent_workflow_run(
        self, *, organization_id: UUID, agent_run_id: UUID, workflow_run_id: UUID
    ) -> AgentRun: ...

    async def list_unprojected_workflow_events(
        self, *, organization_id: UUID, limit: int
    ) -> tuple[LinkedWorkflowEvent, ...]: ...

    async def project_workflow_event(
        self,
        *,
        item: LinkedWorkflowEvent,
        blocks: tuple[dict[str, Any], ...],
        status: str | None,
        safe_error_code: str | None,
    ) -> bool: ...

    async def resume_confirmed_team_followups(
        self, *, organization_id: UUID, limit: int
    ) -> int: ...

    async def assert_job_claim(self, *, job: AssistantJob) -> None: ...

    async def finish_agent_run(self, *, run: AgentRun) -> None: ...

    async def append_handoff(self, *, handoff: AgentHandoffRecord) -> None: ...

    async def save_checkpoint(self, *, checkpoint: AgentCheckpoint) -> None: ...

    async def load_orchestration_checkpoint(
        self, *, organization_id: UUID, orchestration_run_id: UUID
    ) -> dict[str, Any] | None: ...

    async def save_orchestration_checkpoint(
        self,
        *,
        organization_id: UUID,
        orchestration_run_id: UUID,
        checkpoint: dict[str, Any],
        execution_plan: dict[str, Any],
    ) -> None: ...

    async def append_assistant_blocks(
        self,
        *,
        job: AssistantJob,
        blocks: tuple[dict[str, Any], ...],
        dedupe_key: str,
    ) -> None: ...

    async def get_tool_invocation(
        self, *, organization_id: UUID, agent_run_id: UUID, dedupe_key: str
    ) -> ToolInvocation | None: ...

    async def append_tool_invocation(self, *, invocation: ToolInvocation) -> None: ...

    async def finish_tool_invocation(
        self,
        *,
        organization_id: UUID,
        invocation_id: UUID,
        status: str,
        typed_output: dict[str, Any],
        context_references: tuple[dict[str, Any], ...],
        safe_error_code: str | None,
    ) -> None: ...

    async def append_agent_model_invocation(self, *, invocation: AgentModelInvocation) -> None: ...

    async def append_event(self, *, event: AssistantEvent) -> AssistantEvent: ...

    async def complete_job(self, *, job_id: UUID, worker_id: str) -> None: ...

    async def fail_job(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        error_code: str,
        next_available_at: datetime,
    ) -> None: ...


class AssistantTransactionFactory(Protocol):
    """Factory that creates tenant-scoped transactions from an actor or UUID."""

    def __call__(self, context: AuthenticatedActor | UUID) -> "AssistantTransaction": ...


class AssistantTransaction(Protocol):
    @property
    def repository(self) -> AssistantRepository: ...

    @property
    def session(self) -> Any: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

    async def __aenter__(self) -> "AssistantTransaction": ...

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None: ...
