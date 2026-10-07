"""PostgreSQL repository for durable Assistant transcript and Agent execution state."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.assistant.adapters.database_models import (
    AgentCheckpointModel,
    AgentHandoffModel,
    AgentModelInvocationModel,
    AgentRunModel,
    AssistantConversationModel,
    AssistantEventModel,
    AssistantJobModel,
    AssistantMessageModel,
    AssistantTurnModel,
    OrchestrationRunModel,
    ToolInvocationModel,
)
from app.modules.assistant.application.ports import (
    AssistantConversationMutationResult,
    AssistantConversationSnapshot,
    AssistantTurnMutationResult,
    ConversationTitleInput,
    LinkedWorkflowEvent,
)
from app.modules.assistant.application.projection_service import advance_pending_followup
from app.modules.assistant.domain.models import (
    AgentCheckpoint,
    AgentHandoffRecord,
    AgentModelInvocation,
    AgentRun,
    AgentRunStatus,
    AssistantConversation,
    AssistantEvent,
    AssistantIdempotencyKeyReusedError,
    AssistantJob,
    AssistantJobStatus,
    AssistantMessage,
    AssistantTurn,
    AssistantTurnStatus,
    ConversationStatus,
    InvocationStatus,
    MessageRole,
    OrchestrationRun,
    OrchestrationRunStatus,
    ToolInvocation,
)
from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.audit.domain.events import AuditOutcome
from app.modules.feedback.adapters.payload_repository import assert_context_live, live_payload
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.planning_runs.adapters.database_models import OutboxEventModel, WorkflowEventModel
from app.modules.planning_runs.domain.models import WorkflowEvent
from app.modules.work.adapters.database_models import IdempotencyRecordModel, IdempotencyState
from app.modules.work.planning.assignment.adapters.database_models import TeamRequirementSetModel
from work_management_ai.agents.orchestrator.contracts import PendingFollowup

_IDEMPOTENCY_TTL = timedelta(hours=24)


def _conversation(model: AssistantConversationModel) -> AssistantConversation:
    return AssistantConversation(
        id=model.id,
        organization_id=model.organization_id,
        owner_membership_id=model.owner_membership_id,
        locale=model.locale,  # type: ignore[arg-type]
        title=model.title,
        status=ConversationStatus(model.status),
        version=model.version,
        last_message_sequence=model.last_message_sequence,
        last_event_sequence=model.last_event_sequence,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _message(model: AssistantMessageModel) -> AssistantMessage:
    return AssistantMessage(
        id=model.id,
        organization_id=model.organization_id,
        conversation_id=model.conversation_id,
        sequence=model.sequence,
        role=MessageRole(model.role),
        content_blocks=tuple(model.content_blocks),
        created_by_membership_id=model.created_by_membership_id,
        turn_id=model.turn_id,
        dedupe_key=model.dedupe_key,
        created_at=model.created_at,
    )


def _turn(model: AssistantTurnModel) -> AssistantTurn:
    return AssistantTurn(
        id=model.id,
        organization_id=model.organization_id,
        conversation_id=model.conversation_id,
        user_message_id=model.user_message_id,
        actor_membership_id=model.actor_membership_id,
        objective=model.objective,
        locale=model.locale,  # type: ignore[arg-type]
        status=AssistantTurnStatus(model.status),
        safe_error_code=model.safe_error_code,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def orchestration_domain(model: OrchestrationRunModel) -> OrchestrationRun:
    return OrchestrationRun(
        id=model.id,
        organization_id=model.organization_id,
        turn_id=model.turn_id,
        trigger_kind=model.trigger_kind,
        actor_membership_id=model.actor_membership_id,
        project_id=model.project_id,
        report_id=model.report_id,
        base_version_id=model.base_version_id,
        snapshot_id=model.snapshot_id,
        snapshot_hash=model.snapshot_hash,
        summary_id=model.summary_id,
        request_key=model.request_key,
        orchestrator_version=model.orchestrator_version,
        orchestrator_fingerprint=model.orchestrator_fingerprint,
        execution_plan=model.execution_plan,
        checkpoint=model.checkpoint,
        budget=model.budget,
        usage=model.usage,
        status=OrchestrationRunStatus(model.status),
        stop_reason=model.stop_reason,
        safe_error_code=model.safe_error_code,
        created_at=model.created_at,
        started_at=model.started_at,
        completed_at=model.completed_at,
        updated_at=model.updated_at,
    )


def _job(model: AssistantJobModel) -> AssistantJob:
    return AssistantJob(
        id=model.id,
        organization_id=model.organization_id,
        conversation_id=model.conversation_id,
        turn_id=model.turn_id,
        orchestration_run_id=model.orchestration_run_id,
        requester_membership_id=model.requester_membership_id,
        job_type=cast(
            Literal["assistant.turn.execute", "assistant.conversation.title"], model.job_type
        ),
        payload=model.payload,
        status=AssistantJobStatus(model.status),
        attempt_count=model.attempt_count,
        max_attempts=model.max_attempts,
        available_at=model.available_at,
        locked_by=model.locked_by,
        lease_until=model.lease_until,
        safe_error_code=model.safe_error_code,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _event(model: AssistantEventModel) -> AssistantEvent:
    return AssistantEvent(
        id=model.id,
        organization_id=model.organization_id,
        conversation_id=model.conversation_id,
        sequence=model.sequence,
        event_type=model.event_type,
        public_payload=model.public_payload,
        turn_id=model.turn_id,
        orchestration_run_id=model.orchestration_run_id,
        agent_run_id=model.agent_run_id,
        source_type=model.source_type,
        source_id=model.source_id,
        source_sequence=model.source_sequence,
        dedupe_key=model.dedupe_key,
        occurred_at=model.occurred_at,
    )


def _agent_run(model: AgentRunModel) -> AgentRun:
    return AgentRun(
        id=model.id,
        organization_id=model.organization_id,
        orchestration_run_id=model.orchestration_run_id,
        parent_agent_run_id=model.parent_agent_run_id,
        inbound_handoff_id=model.inbound_handoff_id,
        agent_id=model.agent_id,
        agent_version=model.agent_version,
        manifest_fingerprint=model.manifest_fingerprint,
        capability=model.capability,
        typed_input=model.typed_input,
        typed_output=model.typed_output,
        version_metadata=model.version_metadata,
        budget=model.budget,
        usage=model.usage,
        status=AgentRunStatus(model.status),
        stop_reason=model.stop_reason,
        safe_error_code=model.safe_error_code,
        workflow_run_id=model.workflow_run_id,
        projected_workflow_sequence=model.projected_workflow_sequence,
        created_at=model.created_at,
        started_at=model.started_at,
        completed_at=model.completed_at,
        updated_at=model.updated_at,
    )


def _tool_invocation(model: ToolInvocationModel) -> ToolInvocation:
    return ToolInvocation(
        id=model.id,
        organization_id=model.organization_id,
        agent_run_id=model.agent_run_id,
        tool_id=model.tool_id,
        tool_version=model.tool_version,
        risk_level=model.risk_level,
        typed_input=model.typed_input,
        typed_output=model.typed_output,
        context_references=tuple(model.context_references),
        status=InvocationStatus(model.status),
        idempotency_key=model.idempotency_key,
        dedupe_key=model.dedupe_key,
        safe_error_code=model.safe_error_code,
        created_at=model.created_at,
        completed_at=model.completed_at,
    )


class PostgreSQLAssistantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_conversation_title_input(
        self, *, actor: AuthenticatedActor, job: AssistantJob
    ) -> ConversationTitleInput | None:
        if (
            job.organization_id != actor.organization_id
            or job.requester_membership_id != actor.membership_id
            or job.job_type != "assistant.conversation.title"
        ):
            return None
        conversation = await self._session.scalar(
            select(AssistantConversationModel).where(
                AssistantConversationModel.organization_id == actor.organization_id,
                AssistantConversationModel.owner_membership_id == actor.membership_id,
                AssistantConversationModel.id == job.conversation_id,
                AssistantConversationModel.status == "ACTIVE",
                AssistantConversationModel.title.is_(None),
            )
        )
        if conversation is None:
            return None
        first = await self._session.scalar(
            select(AssistantMessageModel).where(
                AssistantMessageModel.organization_id == actor.organization_id,
                AssistantMessageModel.conversation_id == job.conversation_id,
                AssistantMessageModel.sequence == 1,
                AssistantMessageModel.role == "USER",
                AssistantMessageModel.turn_id == job.turn_id,
            )
        )
        if first is None:
            return None
        message = " ".join(
            str(block["text"]) for block in first.content_blocks if block.get("kind") == "text"
        ).strip()
        turn = await self._session.scalar(
            select(AssistantTurnModel).where(
                AssistantTurnModel.organization_id == actor.organization_id,
                AssistantTurnModel.id == job.turn_id,
            )
        )
        if not message or turn is None:
            return None
        return ConversationTitleInput(message, turn.locale)  # type: ignore[arg-type]

    async def set_conversation_title(
        self,
        *,
        actor: AuthenticatedActor,
        job: AssistantJob,
        title: str,
        metadata: dict[str, object],
    ) -> bool:
        conversation = await self._session.scalar(
            select(AssistantConversationModel)
            .where(
                AssistantConversationModel.organization_id == actor.organization_id,
                AssistantConversationModel.owner_membership_id == actor.membership_id,
                AssistantConversationModel.id == job.conversation_id,
            )
            .with_for_update()
        )
        # Revalidate after the model call. The lock also serializes event sequence allocation.
        if (
            conversation is None
            or await self.get_conversation_title_input(actor=actor, job=job) is None
        ):
            return False
        if not title.strip() or len(title) > 100:
            raise ValueError("CONVERSATION_TITLE_INVALID")
        now = datetime.now(UTC)
        conversation.title = title
        event = AssistantEvent(
            id=uuid4(),
            organization_id=actor.organization_id,
            conversation_id=job.conversation_id,
            sequence=conversation.last_event_sequence + 1,
            event_type="assistant.conversation.titled.v1",
            public_payload={"conversation_id": str(job.conversation_id)},
            dedupe_key=f"conversation-title:{job.conversation_id}",
            occurred_at=now,
        )
        await self.append_event(event=event)
        self._session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=actor.organization_id,
                actor_membership_id=actor.membership_id,
                action="assistant.conversation.titled",
                outcome=AuditOutcome.SUCCEEDED,
                resource_type="assistant_conversation",
                resource_id=job.conversation_id,
                request_id=str(job.id),
                idempotency_key=f"conversation-title:{job.conversation_id}",
                before_data={},
                after_data={"status": "COMMITTED"},
                reason_data=metadata,
            )
        )
        self._session.add(
            OutboxEventModel(
                id=event.id,
                organization_id=actor.organization_id,
                event_id=event.id,
                event_type=event.event_type,
                aggregate_type="assistant_conversation",
                aggregate_id=job.conversation_id,
                envelope_version="1.0",
                payload={"conversation_id": str(job.conversation_id)},
                occurred_at=now,
            )
        )
        await self._session.flush()
        return True

    async def _idempotency(
        self,
        *,
        actor: AuthenticatedActor,
        operation: str,
        key: str,
        fingerprint: str,
    ) -> IdempotencyRecordModel | None:
        record = await self._session.scalar(
            select(IdempotencyRecordModel).where(
                IdempotencyRecordModel.organization_id == actor.organization_id,
                IdempotencyRecordModel.actor_membership_id == actor.membership_id,
                IdempotencyRecordModel.operation == operation,
                IdempotencyRecordModel.idempotency_key == key,
            )
        )
        if record is None:
            return None
        if (
            record.request_fingerprint != fingerprint
            or record.state is not IdempotencyState.COMPLETED
            or record.response_body is None
        ):
            raise AssistantIdempotencyKeyReusedError("IDEMPOTENCY_KEY_REUSED")
        return record

    def _new_idempotency(
        self,
        *,
        actor: AuthenticatedActor,
        operation: str,
        key: str,
        fingerprint: str,
        now: datetime,
    ) -> IdempotencyRecordModel:
        model = IdempotencyRecordModel(
            id=uuid4(),
            organization_id=actor.organization_id,
            actor_membership_id=actor.membership_id,
            operation=operation,
            idempotency_key=key,
            request_fingerprint=fingerprint,
            state=IdempotencyState.IN_PROGRESS,
            response_status=None,
            response_body=None,
            created_at=now,
            expires_at=now + _IDEMPOTENCY_TTL,
        )
        self._session.add(model)
        return model

    def _audit(
        self,
        *,
        actor: AuthenticatedActor,
        action: str,
        resource_type: str,
        resource_id: UUID,
        request_id: str,
        idempotency_key: str,
    ) -> None:
        self._session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=actor.organization_id,
                actor_membership_id=actor.membership_id,
                action=action,
                outcome=AuditOutcome.SUCCEEDED,
                resource_type=resource_type,
                resource_id=resource_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                before_data={},
                after_data={"status": "COMMITTED"},
                reason_data={},
            )
        )

    async def create_conversation_mutation(
        self,
        *,
        actor: AuthenticatedActor,
        conversation: AssistantConversation,
        request_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> AssistantConversationMutationResult:
        operation = "assistant.conversation.create"
        replay = await self._idempotency(
            actor=actor,
            operation=operation,
            key=idempotency_key,
            fingerprint=request_fingerprint,
        )
        if replay is not None:
            response_body = replay.response_body
            if response_body is None:
                raise AssistantDomainLookupError("ASSISTANT_REPLAY_NOT_FOUND")
            conversation_id = UUID(str(response_body["conversation_id"]))
            model = await self._session.scalar(
                select(AssistantConversationModel).where(
                    AssistantConversationModel.organization_id == actor.organization_id,
                    AssistantConversationModel.owner_membership_id == actor.membership_id,
                    AssistantConversationModel.id == conversation_id,
                )
            )
            if model is None:
                raise AssistantDomainLookupError("ASSISTANT_REPLAY_NOT_FOUND")
            return AssistantConversationMutationResult(
                conversation=_conversation(model), replayed=True
            )
        if (
            conversation.organization_id != actor.organization_id
            or conversation.owner_membership_id != actor.membership_id
        ):
            raise AssistantDomainLookupError("ASSISTANT_CONVERSATION_NOT_FOUND")
        now = datetime.now(UTC)
        record = self._new_idempotency(
            actor=actor,
            operation=operation,
            key=idempotency_key,
            fingerprint=request_fingerprint,
            now=now,
        )
        self._session.add(
            AssistantConversationModel(
                id=conversation.id,
                organization_id=conversation.organization_id,
                owner_membership_id=conversation.owner_membership_id,
                locale=conversation.locale,
                title=conversation.title,
                status=conversation.status.value,
                version=conversation.version,
                last_message_sequence=conversation.last_message_sequence,
                last_event_sequence=conversation.last_event_sequence,
                created_at=conversation.created_at,
                updated_at=conversation.updated_at,
            )
        )
        self._audit(
            actor=actor,
            action="assistant.conversation.created",
            resource_type="assistant_conversation",
            resource_id=conversation.id,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
        record.state = IdempotencyState.COMPLETED
        record.response_status = 201
        record.response_body = {"conversation_id": str(conversation.id)}
        await self._session.flush()
        return AssistantConversationMutationResult(conversation=conversation, replayed=False)

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
    ) -> AssistantTurnMutationResult:
        operation = f"assistant.message.submit:{message.conversation_id}"
        replay = await self._idempotency(
            actor=actor,
            operation=operation,
            key=idempotency_key,
            fingerprint=request_fingerprint,
        )
        if replay is not None:
            response_body = replay.response_body
            if response_body is None:
                raise AssistantDomainLookupError("ASSISTANT_REPLAY_NOT_FOUND")
            return await self._load_turn_result(
                actor=actor,
                message_id=UUID(str(response_body["message_id"])),
                turn_id=UUID(str(response_body["turn_id"])),
                replayed=True,
            )
        identifiers = {
            message.organization_id,
            turn.organization_id,
            run.organization_id,
            job.organization_id,
            event.organization_id,
        }
        if (
            identifiers != {actor.organization_id}
            or turn.actor_membership_id != actor.membership_id
        ):
            raise AssistantDomainLookupError("ASSISTANT_CONVERSATION_NOT_FOUND")
        conversation_model = await self._session.scalar(
            select(AssistantConversationModel)
            .where(
                AssistantConversationModel.organization_id == actor.organization_id,
                AssistantConversationModel.owner_membership_id == actor.membership_id,
                AssistantConversationModel.id == message.conversation_id,
                AssistantConversationModel.status == ConversationStatus.ACTIVE.value,
            )
            .with_for_update()
        )
        if conversation_model is None:
            raise AssistantDomainLookupError("ASSISTANT_CONVERSATION_NOT_FOUND")
        message = replace(
            message,
            sequence=conversation_model.last_message_sequence + 1,
        )
        event = replace(
            event,
            sequence=conversation_model.last_event_sequence + 1,
        )
        if (
            message.turn_id != turn.id
            or turn.user_message_id != message.id
            or run.turn_id != turn.id
            or job.orchestration_run_id != run.id
            or event.turn_id != turn.id
            or event.orchestration_run_id != run.id
        ):
            raise AssistantDomainLookupError("ASSISTANT_GRAPH_IDENTITY_INVALID")
        now = datetime.now(UTC)
        record = self._new_idempotency(
            actor=actor,
            operation=operation,
            key=idempotency_key,
            fingerprint=request_fingerprint,
            now=now,
        )
        message_model = AssistantMessageModel(
            id=message.id,
            organization_id=message.organization_id,
            conversation_id=message.conversation_id,
            sequence=message.sequence,
            role=message.role.value,
            content_blocks=list(message.content_blocks),
            created_by_membership_id=message.created_by_membership_id,
            turn_id=message.turn_id,
            dedupe_key=message.dedupe_key,
            created_at=message.created_at,
        )
        self._session.add(message_model)
        await self._session.flush()

        turn_model = AssistantTurnModel(
            id=turn.id,
            organization_id=turn.organization_id,
            conversation_id=turn.conversation_id,
            user_message_id=turn.user_message_id,
            actor_membership_id=turn.actor_membership_id,
            objective=turn.objective,
            locale=turn.locale,
            status=turn.status.value,
            safe_error_code=turn.safe_error_code,
            created_at=turn.created_at,
            updated_at=turn.updated_at,
        )
        self._session.add(turn_model)
        await self._session.flush()

        run_model = OrchestrationRunModel(
            id=run.id,
            organization_id=run.organization_id,
            turn_id=run.turn_id,
            actor_membership_id=turn.actor_membership_id,
            orchestrator_version=run.orchestrator_version,
            orchestrator_fingerprint=run.orchestrator_fingerprint,
            execution_plan=run.execution_plan,
            checkpoint=run.checkpoint,
            budget=run.budget,
            usage=run.usage,
            status=run.status.value,
            stop_reason=run.stop_reason,
            safe_error_code=run.safe_error_code,
            created_at=run.created_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
            updated_at=run.updated_at,
        )
        self._session.add(run_model)
        await self._session.flush()

        self._session.add_all(
            [
                AssistantJobModel(
                    id=job.id,
                    organization_id=job.organization_id,
                    conversation_id=job.conversation_id,
                    turn_id=job.turn_id,
                    orchestration_run_id=job.orchestration_run_id,
                    requester_membership_id=job.requester_membership_id,
                    job_type=job.job_type,
                    payload=job.payload,
                    status=job.status.value,
                    attempt_count=job.attempt_count,
                    max_attempts=job.max_attempts,
                    available_at=job.available_at,
                    locked_by=job.locked_by,
                    lease_until=job.lease_until,
                    safe_error_code=job.safe_error_code,
                    created_at=job.created_at,
                    updated_at=job.updated_at,
                ),
                AssistantEventModel(
                    id=event.id,
                    organization_id=event.organization_id,
                    conversation_id=event.conversation_id,
                    sequence=event.sequence,
                    event_type=event.event_type,
                    public_payload=event.public_payload,
                    turn_id=event.turn_id,
                    orchestration_run_id=event.orchestration_run_id,
                    agent_run_id=event.agent_run_id,
                    source_type=event.source_type,
                    source_id=event.source_id,
                    source_sequence=event.source_sequence,
                    dedupe_key=event.dedupe_key,
                    occurred_at=event.occurred_at,
                ),
            ]
        )
        if message.sequence == 1 and not conversation_model.title and turn.objective.strip():
            self._session.add(
                AssistantJobModel(
                    id=uuid4(),
                    organization_id=job.organization_id,
                    conversation_id=job.conversation_id,
                    turn_id=job.turn_id,
                    orchestration_run_id=job.orchestration_run_id,
                    requester_membership_id=actor.membership_id,
                    job_type="assistant.conversation.title",
                    payload={},
                    status="QUEUED",
                    attempt_count=0,
                    max_attempts=3,
                    available_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        conversation_model.last_message_sequence = message.sequence
        conversation_model.last_event_sequence = event.sequence
        conversation_model.version += 2
        conversation_model.updated_at = now
        self._audit(
            actor=actor,
            action="assistant.message.submitted",
            resource_type="assistant_turn",
            resource_id=turn.id,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
        record.state = IdempotencyState.COMPLETED
        record.response_status = 202
        record.response_body = {"message_id": str(message.id), "turn_id": str(turn.id)}
        await self._session.flush()
        return AssistantTurnMutationResult(
            message=message, turn=turn, run=run, job=job, event=event, replayed=False
        )

    async def _load_turn_result(
        self,
        *,
        actor: AuthenticatedActor,
        message_id: UUID,
        turn_id: UUID,
        replayed: bool,
    ) -> AssistantTurnMutationResult:
        message_model = await self._session.scalar(
            select(AssistantMessageModel).where(
                AssistantMessageModel.organization_id == actor.organization_id,
                AssistantMessageModel.id == message_id,
            )
        )
        turn_model = await self._session.scalar(
            select(AssistantTurnModel).where(
                AssistantTurnModel.organization_id == actor.organization_id,
                AssistantTurnModel.id == turn_id,
            )
        )
        run_model = await self._session.scalar(
            select(OrchestrationRunModel).where(
                OrchestrationRunModel.organization_id == actor.organization_id,
                OrchestrationRunModel.turn_id == turn_id,
            )
        )
        if message_model is None or turn_model is None or run_model is None:
            raise AssistantDomainLookupError("ASSISTANT_REPLAY_NOT_FOUND")
        job_model = await self._session.scalar(
            select(AssistantJobModel).where(
                AssistantJobModel.organization_id == actor.organization_id,
                AssistantJobModel.orchestration_run_id == run_model.id,
                AssistantJobModel.job_type == "assistant.turn.execute",
            )
        )
        event_model = await self._session.scalar(
            select(AssistantEventModel).where(
                AssistantEventModel.organization_id == actor.organization_id,
                AssistantEventModel.turn_id == turn_id,
                AssistantEventModel.event_type == "assistant.turn.queued.v1",
            )
        )
        if job_model is None or event_model is None:
            raise AssistantDomainLookupError("ASSISTANT_REPLAY_NOT_FOUND")
        return AssistantTurnMutationResult(
            message=_message(message_model),
            turn=_turn(turn_model),
            run=orchestration_domain(run_model),
            job=_job(job_model),
            event=_event(event_model),
            replayed=replayed,
        )

    async def get_conversation_snapshot(
        self, *, actor: AuthenticatedActor, conversation_id: UUID
    ) -> AssistantConversationSnapshot | None:
        conversation_model = await self._session.scalar(
            select(AssistantConversationModel).where(
                AssistantConversationModel.organization_id == actor.organization_id,
                AssistantConversationModel.owner_membership_id == actor.membership_id,
                AssistantConversationModel.id == conversation_id,
            )
        )
        if conversation_model is None:
            return None
        message_models = await self._session.scalars(
            select(AssistantMessageModel)
            .where(
                AssistantMessageModel.organization_id == actor.organization_id,
                AssistantMessageModel.conversation_id == conversation_id,
            )
            .order_by(AssistantMessageModel.sequence)
        )
        turn_models = await self._session.scalars(
            select(AssistantTurnModel)
            .where(
                AssistantTurnModel.organization_id == actor.organization_id,
                AssistantTurnModel.conversation_id == conversation_id,
                live_payload(AssistantTurnModel, "assistant_turns"),
            )
            .order_by(AssistantTurnModel.created_at, AssistantTurnModel.id)
        )
        turns = tuple(_turn(model) for model in turn_models)
        turn_ids = [turn.id for turn in turns]
        run_models = (
            await self._session.scalars(
                select(OrchestrationRunModel)
                .where(
                    OrchestrationRunModel.organization_id == actor.organization_id,
                    OrchestrationRunModel.turn_id.in_(turn_ids),
                    live_payload(OrchestrationRunModel, "orchestration_runs"),
                )
                .order_by(OrchestrationRunModel.created_at, OrchestrationRunModel.id)
            )
            if turn_ids
            else ()
        )
        event_models = await self._session.scalars(
            select(AssistantEventModel)
            .where(
                AssistantEventModel.organization_id == actor.organization_id,
                AssistantEventModel.conversation_id == conversation_id,
                live_payload(AssistantEventModel, "assistant_events"),
            )
            .order_by(AssistantEventModel.sequence)
        )
        return AssistantConversationSnapshot(
            conversation=_conversation(conversation_model),
            messages=tuple(_message(model) for model in message_models),
            turns=turns,
            orchestration_runs=tuple(orchestration_domain(model) for model in run_models),
            events=tuple(_event(model) for model in event_models),
        )

    async def list_conversations(
        self, *, actor: AuthenticatedActor, limit: int
    ) -> list[AssistantConversation]:
        models = await self._session.scalars(
            select(AssistantConversationModel)
            .where(
                AssistantConversationModel.organization_id == actor.organization_id,
                AssistantConversationModel.owner_membership_id == actor.membership_id,
            )
            .order_by(
                AssistantConversationModel.updated_at.desc(),
                AssistantConversationModel.id.desc(),
            )
            .limit(limit)
        )
        return [_conversation(m) for m in models]

    async def list_events(
        self,
        *,
        actor: AuthenticatedActor,
        conversation_id: UUID,
        after_sequence: int,
    ) -> list[AssistantEvent]:
        # Verify owner before listing
        conv = await self._session.scalar(
            select(AssistantConversationModel).where(
                AssistantConversationModel.organization_id == actor.organization_id,
                AssistantConversationModel.owner_membership_id == actor.membership_id,
                AssistantConversationModel.id == conversation_id,
            )
        )
        if conv is None:
            return []
        models = await self._session.scalars(
            select(AssistantEventModel)
            .where(
                AssistantEventModel.organization_id == actor.organization_id,
                AssistantEventModel.conversation_id == conversation_id,
                live_payload(AssistantEventModel, "assistant_events"),
                AssistantEventModel.sequence > after_sequence,
            )
            .order_by(AssistantEventModel.sequence)
        )
        return [_event(m) for m in models]

    async def append_rejected_audit(
        self,
        *,
        actor: AuthenticatedActor,
        action: str,
        resource_type: str,
        resource_id: UUID | None,
        request_id: str,
        reason_code: str,
    ) -> None:
        self._session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=actor.organization_id,
                actor_membership_id=actor.membership_id,
                action=action,
                outcome=AuditOutcome.REJECTED,
                resource_type=resource_type,
                resource_id=resource_id,
                request_id=request_id,
                idempotency_key=None,
                before_data={},
                after_data={},
                reason_data={"code": reason_code},
            )
        )
        await self._session.flush()

    async def claim_job(
        self,
        *,
        organization_id: UUID,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
        job_type: str = "assistant.turn.execute",
    ) -> AssistantJob | None:
        model = await self._session.scalar(
            select(AssistantJobModel)
            .where(
                AssistantJobModel.organization_id == organization_id,
                AssistantJobModel.job_type == job_type,
                live_payload(AssistantJobModel, "assistant_jobs"),
                AssistantJobModel.attempt_count < AssistantJobModel.max_attempts,
                AssistantJobModel.available_at <= now,
                (AssistantJobModel.status == AssistantJobStatus.QUEUED.value)
                | (
                    (AssistantJobModel.status == AssistantJobStatus.RUNNING.value)
                    & (AssistantJobModel.lease_until < now)
                ),
            )
            .order_by(AssistantJobModel.created_at, AssistantJobModel.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if model is None:
            return None
        model.status = AssistantJobStatus.RUNNING.value
        model.attempt_count += 1
        model.locked_by = worker_id
        model.lease_until = lease_until
        model.updated_at = now
        await self._session.flush()
        return _job(model)

    async def begin_orchestration(self, *, job: AssistantJob) -> OrchestrationRun:
        await assert_context_live(
            self._session, job.organization_id, "orchestration_runs", job.orchestration_run_id
        )
        model = await self._session.scalar(
            select(OrchestrationRunModel)
            .where(
                OrchestrationRunModel.organization_id == job.organization_id,
                OrchestrationRunModel.id == job.orchestration_run_id,
            )
            .with_for_update()
        )
        if model is None:
            raise AssistantDomainLookupError("ORCHESTRATION_RUN_NOT_FOUND")
        current = orchestration_domain(model)
        if current.status is not OrchestrationRunStatus.QUEUED:
            return current
        run = current.mark_running()
        model.status = run.status.value
        model.started_at = run.started_at
        model.updated_at = run.updated_at
        await self._session.execute(
            update(AssistantTurnModel)
            .where(
                AssistantTurnModel.organization_id == job.organization_id,
                AssistantTurnModel.id == job.turn_id,
                AssistantTurnModel.status == AssistantTurnStatus.QUEUED.value,
            )
            .values(status=AssistantTurnStatus.RUNNING.value, updated_at=run.updated_at)
        )
        await self._session.flush()
        return run

    async def finish_orchestration(
        self,
        *,
        job: AssistantJob,
        status: str,
        stop_reason: str,
        safe_error_code: str | None,
    ) -> None:
        if status not in {"COMPLETED", "AWAITING_INPUT", "AWAITING_HUMAN", "FAILED"}:
            raise AssistantDomainLookupError("ORCHESTRATION_STATUS_INVALID")
        now = datetime.now(UTC)
        run_result = await self._session.execute(
            update(OrchestrationRunModel)
            .where(
                OrchestrationRunModel.organization_id == job.organization_id,
                OrchestrationRunModel.id == job.orchestration_run_id,
                OrchestrationRunModel.status == OrchestrationRunStatus.RUNNING.value,
            )
            .values(
                status=status,
                stop_reason=stop_reason,
                safe_error_code=safe_error_code,
                completed_at=now if status in {"COMPLETED", "FAILED"} else None,
                updated_at=now,
            )
        )
        turn_result = await self._session.execute(
            update(AssistantTurnModel)
            .where(
                AssistantTurnModel.organization_id == job.organization_id,
                AssistantTurnModel.id == job.turn_id,
                AssistantTurnModel.status.in_(
                    (AssistantTurnStatus.QUEUED.value, AssistantTurnStatus.RUNNING.value)
                ),
            )
            .values(status=status, safe_error_code=safe_error_code, updated_at=now)
        )
        if run_result.rowcount != 1 or turn_result.rowcount != 1:  # type: ignore[attr-defined]
            raise AssistantDomainLookupError("ORCHESTRATION_FINALIZE_CONFLICT")

    async def append_agent_run(self, *, run: AgentRun) -> AgentRun:
        self._session.add(
            AgentRunModel(
                id=run.id,
                organization_id=run.organization_id,
                orchestration_run_id=run.orchestration_run_id,
                parent_agent_run_id=run.parent_agent_run_id,
                inbound_handoff_id=run.inbound_handoff_id,
                agent_id=run.agent_id,
                agent_version=run.agent_version,
                manifest_fingerprint=run.manifest_fingerprint,
                capability=run.capability,
                typed_input=run.typed_input,
                typed_output=run.typed_output,
                version_metadata=run.version_metadata,
                budget=run.budget,
                usage=run.usage,
                status=run.status.value,
                stop_reason=run.stop_reason,
                safe_error_code=run.safe_error_code,
                workflow_run_id=run.workflow_run_id,
                projected_workflow_sequence=run.projected_workflow_sequence,
                created_at=run.created_at,
                started_at=run.started_at,
                completed_at=run.completed_at,
                updated_at=run.updated_at,
            )
        )
        await self._session.flush()
        return run

    async def get_agent_run(self, *, organization_id: UUID, run_id: UUID) -> AgentRun | None:
        await assert_context_live(self._session, organization_id, "agent_runs", run_id)
        model = await self._session.scalar(
            select(AgentRunModel).where(
                AgentRunModel.organization_id == organization_id,
                AgentRunModel.id == run_id,
            )
        )
        return _agent_run(model) if model is not None else None

    async def get_agent_run_turn_context(
        self, *, organization_id: UUID, run_id: UUID
    ) -> tuple[AgentRun, UUID] | None:
        row = (
            await self._session.execute(
                select(AgentRunModel, OrchestrationRunModel.turn_id)
                .join(
                    OrchestrationRunModel,
                    (OrchestrationRunModel.organization_id == AgentRunModel.organization_id)
                    & (OrchestrationRunModel.id == AgentRunModel.orchestration_run_id),
                )
                .where(
                    AgentRunModel.organization_id == organization_id,
                    AgentRunModel.id == run_id,
                    AgentRunModel.agent_id == "planning",
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return _agent_run(row[0]), row[1]

    async def get_accepted_planning_action(
        self, *, organization_id: UUID, turn_id: UUID
    ) -> dict[str, object] | None:
        blocks = await self._session.scalar(
            select(AssistantMessageModel.content_blocks)
            .join(
                AssistantTurnModel,
                (AssistantTurnModel.organization_id == AssistantMessageModel.organization_id)
                & (AssistantTurnModel.user_message_id == AssistantMessageModel.id),
            )
            .where(
                AssistantTurnModel.organization_id == organization_id,
                AssistantTurnModel.id == turn_id,
                AssistantMessageModel.organization_id == organization_id,
                AssistantMessageModel.role == MessageRole.USER.value,
            )
        )
        if blocks is None:
            return None
        for block in blocks:
            if block.get("kind") == "accepted_card_action":
                action = block.get("action")
                return cast(dict[str, object], action) if isinstance(action, dict) else None
            if block.get("kind") in {"PLANNING_INPUT", "PLANNING_REVISE"}:
                return cast(dict[str, object], block)
        return None

    async def link_agent_workflow_run(
        self, *, organization_id: UUID, agent_run_id: UUID, workflow_run_id: UUID
    ) -> AgentRun:
        model = await self._session.scalar(
            select(AgentRunModel)
            .where(
                AgentRunModel.organization_id == organization_id,
                AgentRunModel.id == agent_run_id,
                AgentRunModel.agent_id == "planning",
            )
            .with_for_update()
        )
        if model is None or model.workflow_run_id not in {None, workflow_run_id}:
            raise AssistantDomainLookupError("AGENT_WORKFLOW_LINK_CONFLICT")
        model.workflow_run_id = workflow_run_id
        if model.projected_workflow_sequence is None:
            model.projected_workflow_sequence = 0
        model.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _agent_run(model)

    async def list_unprojected_workflow_events(
        self, *, organization_id: UUID, limit: int
    ) -> tuple[LinkedWorkflowEvent, ...]:
        rows = (
            await self._session.execute(
                select(
                    AgentRunModel,
                    OrchestrationRunModel.turn_id,
                    AssistantTurnModel.conversation_id,
                    WorkflowEventModel,
                )
                .join(
                    OrchestrationRunModel,
                    (OrchestrationRunModel.organization_id == AgentRunModel.organization_id)
                    & (OrchestrationRunModel.id == AgentRunModel.orchestration_run_id),
                )
                .join(
                    AssistantTurnModel,
                    (AssistantTurnModel.organization_id == OrchestrationRunModel.organization_id)
                    & (AssistantTurnModel.id == OrchestrationRunModel.turn_id),
                )
                .join(
                    WorkflowEventModel,
                    (WorkflowEventModel.organization_id == AgentRunModel.organization_id)
                    & (WorkflowEventModel.workflow_run_id == AgentRunModel.workflow_run_id)
                    & (
                        WorkflowEventModel.sequence
                        > func.coalesce(AgentRunModel.projected_workflow_sequence, 0)
                    ),
                )
                .where(
                    AgentRunModel.organization_id == organization_id,
                    AgentRunModel.agent_id == "planning",
                    AgentRunModel.workflow_run_id.is_not(None),
                    OrchestrationRunModel.organization_id == organization_id,
                    AssistantTurnModel.organization_id == organization_id,
                    WorkflowEventModel.organization_id == organization_id,
                    live_payload(WorkflowEventModel, "workflow_events"),
                    live_payload(AgentRunModel, "agent_runs"),
                )
                .order_by(AgentRunModel.created_at, AgentRunModel.id, WorkflowEventModel.sequence)
                .limit(limit)
                .with_for_update(of=AgentRunModel, skip_locked=True)
            )
        ).all()
        return tuple(
            LinkedWorkflowEvent(
                agent_run=_agent_run(row[0]),
                turn_id=row[1],
                conversation_id=row[2],
                event=WorkflowEvent(
                    id=row[3].id,
                    organization_id=row[3].organization_id,
                    workflow_run_id=row[3].workflow_run_id,
                    sequence=row[3].sequence,
                    event_type=row[3].event_type,
                    public_payload=row[3].public_payload,
                    created_at=row[3].created_at,
                ),
            )
            for row in rows
        )

    async def project_workflow_event(
        self,
        *,
        item: LinkedWorkflowEvent,
        blocks: tuple[dict[str, object], ...],
        status: str | None,
        safe_error_code: str | None,
    ) -> bool:
        await assert_context_live(
            self._session, item.event.organization_id, "workflow_events", item.event.id
        )
        await assert_context_live(
            self._session, item.agent_run.organization_id, "agent_runs", item.agent_run.id
        )
        event = item.event
        organization_id = item.agent_run.organization_id
        if (
            event.organization_id != organization_id
            or event.workflow_run_id != item.agent_run.workflow_run_id
        ):
            return False
        agent = await self._session.scalar(
            select(AgentRunModel)
            .where(
                AgentRunModel.organization_id == organization_id,
                AgentRunModel.id == item.agent_run.id,
                AgentRunModel.workflow_run_id == event.workflow_run_id,
            )
            .with_for_update()
        )
        if agent is None or (agent.projected_workflow_sequence or 0) >= event.sequence:
            return False
        conversation = await self._session.scalar(
            select(AssistantConversationModel)
            .where(
                AssistantConversationModel.organization_id == organization_id,
                AssistantConversationModel.id == item.conversation_id,
            )
            .with_for_update()
        )
        if conversation is None:
            return False
        checkpoint_value = await self._session.scalar(
            select(OrchestrationRunModel.checkpoint).where(
                OrchestrationRunModel.organization_id == organization_id,
                OrchestrationRunModel.id == item.agent_run.orchestration_run_id,
            )
        )
        original_checkpoint = dict(checkpoint_value or {})
        checkpoint, should_resume = advance_pending_followup(original_checkpoint, event)
        if should_resume:
            blocks = tuple(
                {**block, "continue_team": True}
                if block.get("kind") == "decision_result"
                else block
                for block in blocks
            )
        dedupe_key = f"workflow:{event.workflow_run_id}:{event.sequence}"
        existing = await self._session.scalar(
            select(AssistantMessageModel.id).where(
                AssistantMessageModel.organization_id == organization_id,
                AssistantMessageModel.conversation_id == item.conversation_id,
                AssistantMessageModel.dedupe_key == dedupe_key,
            )
        )
        now = datetime.now(UTC)
        if existing is None:
            message_id = uuid4()
            message_sequence = conversation.last_message_sequence + 1
            assistant_event_sequence = conversation.last_event_sequence + 1
            self._session.add(
                AssistantMessageModel(
                    id=message_id,
                    organization_id=organization_id,
                    conversation_id=item.conversation_id,
                    sequence=message_sequence,
                    role=MessageRole.ASSISTANT.value,
                    content_blocks=list(blocks),
                    created_by_membership_id=None,
                    turn_id=item.turn_id,
                    dedupe_key=dedupe_key,
                    created_at=now,
                )
            )
            self._session.add(
                AssistantEventModel(
                    id=uuid4(),
                    organization_id=organization_id,
                    conversation_id=item.conversation_id,
                    sequence=assistant_event_sequence,
                    event_type="assistant.workflow.projected.v1",
                    public_payload={
                        "turn_id": str(item.turn_id),
                        "agent_run_id": str(item.agent_run.id),
                        "workflow_run_id": str(event.workflow_run_id),
                        "workflow_sequence": event.sequence,
                        "message_id": str(message_id),
                        "message_sequence": message_sequence,
                    },
                    turn_id=item.turn_id,
                    orchestration_run_id=item.agent_run.orchestration_run_id,
                    agent_run_id=item.agent_run.id,
                    source_type="WORKFLOW_EVENT",
                    source_id=event.workflow_run_id,
                    source_sequence=event.sequence,
                    dedupe_key=f"event:{dedupe_key}",
                    occurred_at=now,
                )
            )
            conversation.last_message_sequence = message_sequence
            conversation.last_event_sequence = assistant_event_sequence
            conversation.version += 1
            conversation.updated_at = now
        agent.projected_workflow_sequence = event.sequence
        agent.updated_at = now
        if status is not None:
            agent.status = status
            agent.safe_error_code = safe_error_code
            if status in {"COMPLETED", "FAILED"}:
                agent.completed_at = now
            await self._session.execute(
                update(OrchestrationRunModel)
                .where(
                    OrchestrationRunModel.organization_id == organization_id,
                    OrchestrationRunModel.id == item.agent_run.orchestration_run_id,
                )
                .values(
                    status=status,
                    safe_error_code=safe_error_code,
                    completed_at=now if status in {"COMPLETED", "FAILED"} else None,
                    updated_at=now,
                )
            )
            await self._session.execute(
                update(AssistantTurnModel)
                .where(
                    AssistantTurnModel.organization_id == organization_id,
                    AssistantTurnModel.id == item.turn_id,
                )
                .values(status=status, safe_error_code=safe_error_code, updated_at=now)
            )
        if checkpoint != original_checkpoint:
            pending_state = None
            raw_pending = checkpoint.get("pending_followup")
            if isinstance(raw_pending, dict):
                pending_values = cast(dict[str, object], raw_pending)
                raw_state = pending_values.get("state")
                pending_state = raw_state if isinstance(raw_state, str) else None
            checkpoint_status = (
                "QUEUED"
                if should_resume
                else ("COMPLETED" if pending_state == "CANCELLED" else "AWAITING_HUMAN")
            )
            await self._session.execute(
                update(OrchestrationRunModel)
                .where(
                    OrchestrationRunModel.organization_id == organization_id,
                    OrchestrationRunModel.id == item.agent_run.orchestration_run_id,
                )
                .values(
                    checkpoint=checkpoint,
                    status=checkpoint_status,
                    completed_at=now if checkpoint_status == "COMPLETED" else None,
                    updated_at=now,
                )
            )
            if should_resume:
                await self._session.execute(
                    update(AssistantTurnModel)
                    .where(
                        AssistantTurnModel.organization_id == organization_id,
                        AssistantTurnModel.id == item.turn_id,
                    )
                    .values(status="QUEUED", safe_error_code=None, updated_at=now)
                )
                await self._session.execute(
                    update(AssistantJobModel)
                    .where(
                        AssistantJobModel.organization_id == organization_id,
                        AssistantJobModel.orchestration_run_id
                        == item.agent_run.orchestration_run_id,
                    )
                    .values(
                        status="QUEUED",
                        available_at=now,
                        locked_by=None,
                        lease_until=None,
                        safe_error_code=None,
                        updated_at=now,
                    )
                )
        await self._session.flush()
        return existing is None

    async def resume_confirmed_team_followups(self, *, organization_id: UUID, limit: int) -> int:
        runs = tuple(
            (
                await self._session.scalars(
                    select(OrchestrationRunModel)
                    .where(
                        OrchestrationRunModel.organization_id == organization_id,
                        OrchestrationRunModel.status == OrchestrationRunStatus.AWAITING_HUMAN.value,
                        live_payload(OrchestrationRunModel, "orchestration_runs"),
                    )
                    .order_by(OrchestrationRunModel.updated_at, OrchestrationRunModel.id)
                    .limit(min(max(limit, 1), 50))
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        resumed = 0
        now = datetime.now(UTC)
        for run in runs:
            raw = dict(run.checkpoint or {}).get("pending_followup")
            if not isinstance(raw, dict):
                continue
            try:
                pending = PendingFollowup.model_validate(raw)
            except ValueError:
                continue
            if (
                pending.state != "WAITING_TEAM_REQUIREMENTS"
                or pending.project_id is None
                or pending.requirement_set_id is None
            ):
                continue
            requirement = await self._session.scalar(
                select(TeamRequirementSetModel).where(
                    TeamRequirementSetModel.organization_id == organization_id,
                    TeamRequirementSetModel.id == pending.requirement_set_id,
                    TeamRequirementSetModel.project_id == pending.project_id,
                    TeamRequirementSetModel.current_version >= pending.requirement_version,
                    TeamRequirementSetModel.status == "CONFIRMED",
                )
            )
            if requirement is None:
                continue
            ready = pending.mark_ready()
            run.checkpoint = {
                **dict(run.checkpoint or {}),
                "pending_followup": ready.model_dump(mode="json"),
            }
            run.status = OrchestrationRunStatus.QUEUED.value
            run.completed_at = None
            run.updated_at = now
            await self._session.execute(
                update(AssistantTurnModel)
                .where(
                    AssistantTurnModel.organization_id == organization_id,
                    AssistantTurnModel.id == run.turn_id,
                )
                .values(
                    status=AssistantTurnStatus.QUEUED.value,
                    safe_error_code=None,
                    updated_at=now,
                )
            )
            await self._session.execute(
                update(AssistantJobModel)
                .where(
                    AssistantJobModel.organization_id == organization_id,
                    AssistantJobModel.orchestration_run_id == run.id,
                )
                .values(
                    status=AssistantJobStatus.QUEUED.value,
                    available_at=now,
                    locked_by=None,
                    lease_until=None,
                    safe_error_code=None,
                    updated_at=now,
                )
            )
            resumed += 1
        await self._session.flush()
        return resumed

    async def assert_job_claim(self, *, job: AssistantJob) -> None:
        await assert_context_live(self._session, job.organization_id, "assistant_jobs", job.id)
        from app.modules.assistant.domain.models import AssistantJobClaimLost

        model = await self._session.scalar(
            select(AssistantJobModel)
            .where(
                AssistantJobModel.organization_id == job.organization_id,
                AssistantJobModel.id == job.id,
            )
            .with_for_update()
        )
        at = await self._session.scalar(text("SELECT clock_timestamp()"))
        if (
            model is None
            or model.status != "RUNNING"
            or model.locked_by != job.locked_by
            or model.attempt_count != job.attempt_count
            or model.lease_until is None
            or not isinstance(at, datetime)
            or model.lease_until <= at
        ):
            raise AssistantJobClaimLost("ASSISTANT_JOB_CLAIM_LOST")

    async def finish_agent_run(self, *, run: AgentRun) -> None:
        model = await self._session.scalar(
            select(AgentRunModel)
            .where(
                AgentRunModel.organization_id == run.organization_id,
                AgentRunModel.id == run.id,
                AgentRunModel.status == AgentRunStatus.RUNNING.value,
            )
            .with_for_update()
        )
        if model is None:
            raise AssistantDomainLookupError("AGENT_RUN_NOT_RUNNING")
        model.typed_output = run.typed_output
        model.usage = {**model.usage, **run.usage}
        model.status = run.status.value
        model.stop_reason = run.stop_reason
        model.safe_error_code = run.safe_error_code
        model.completed_at = run.completed_at
        model.updated_at = run.updated_at
        await self._session.flush()

    async def resume_agent_run(self, *, run: AgentRun) -> AgentRun:
        model = await self._session.scalar(
            select(AgentRunModel)
            .where(
                AgentRunModel.organization_id == run.organization_id,
                AgentRunModel.id == run.id,
                AgentRunModel.status.in_(
                    (AgentRunStatus.AWAITING_INPUT.value, AgentRunStatus.AWAITING_HUMAN.value)
                ),
            )
            .with_for_update()
        )
        if model is None or run.status is not AgentRunStatus.RUNNING:
            raise AssistantDomainLookupError("AGENT_RUN_NOT_AWAITING")
        model.status = run.status.value
        model.stop_reason = run.stop_reason
        model.safe_error_code = run.safe_error_code
        model.updated_at = run.updated_at
        await self._session.flush()
        return _agent_run(model)

    async def append_handoff(self, *, handoff: AgentHandoffRecord) -> None:
        self._session.add(
            AgentHandoffModel(
                id=handoff.id,
                organization_id=handoff.organization_id,
                orchestration_run_id=handoff.orchestration_run_id,
                parent_agent_run_id=handoff.parent_agent_run_id,
                target_agent_id=handoff.target_agent_id,
                target_agent_version=handoff.target_agent_version,
                capability=handoff.capability,
                objective=handoff.objective,
                typed_input=handoff.typed_input,
                context_references=list(handoff.context_references),
                budget=handoff.budget,
                step_id=handoff.step_id,
                idempotency_key=handoff.idempotency_key,
                dedupe_key=handoff.dedupe_key,
                created_at=handoff.created_at,
            )
        )
        await self._session.flush()

    async def save_checkpoint(self, *, checkpoint: AgentCheckpoint) -> None:
        self._session.add(
            AgentCheckpointModel(
                id=checkpoint.id,
                organization_id=checkpoint.organization_id,
                orchestration_run_id=checkpoint.orchestration_run_id,
                agent_run_id=checkpoint.agent_run_id,
                sequence=checkpoint.sequence,
                node=checkpoint.node,
                typed_state=checkpoint.typed_state,
                checkpoint_version=checkpoint.checkpoint_version,
                created_at=checkpoint.created_at,
            )
        )
        await self._session.flush()

    async def load_orchestration_checkpoint(
        self, *, organization_id: UUID, orchestration_run_id: UUID
    ) -> dict[str, object] | None:
        await assert_context_live(
            self._session, organization_id, "orchestration_runs", orchestration_run_id
        )
        return await self._session.scalar(
            select(OrchestrationRunModel.checkpoint).where(
                OrchestrationRunModel.organization_id == organization_id,
                OrchestrationRunModel.id == orchestration_run_id,
            )
        )

    async def save_orchestration_checkpoint(
        self,
        *,
        organization_id: UUID,
        orchestration_run_id: UUID,
        checkpoint: dict[str, object],
        execution_plan: dict[str, object],
    ) -> None:
        await assert_context_live(
            self._session, organization_id, "orchestration_runs", orchestration_run_id
        )
        result = await self._session.execute(
            update(OrchestrationRunModel)
            .where(
                OrchestrationRunModel.organization_id == organization_id,
                OrchestrationRunModel.id == orchestration_run_id,
            )
            .values(
                checkpoint=checkpoint,
                execution_plan=OrchestrationRunModel.execution_plan.op("||")(execution_plan),
            )
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise AssistantDomainLookupError("ORCHESTRATION_RUN_NOT_FOUND")

    async def append_assistant_blocks(
        self,
        *,
        job: AssistantJob,
        blocks: tuple[dict[str, object], ...],
        dedupe_key: str,
    ) -> None:
        existing = await self._session.scalar(
            select(AssistantMessageModel.id).where(
                AssistantMessageModel.organization_id == job.organization_id,
                AssistantMessageModel.conversation_id == job.conversation_id,
                AssistantMessageModel.dedupe_key == dedupe_key,
            )
        )
        if existing is not None:
            return
        conversation = await self._session.scalar(
            select(AssistantConversationModel)
            .where(
                AssistantConversationModel.organization_id == job.organization_id,
                AssistantConversationModel.id == job.conversation_id,
            )
            .with_for_update()
        )
        if conversation is None:
            raise AssistantDomainLookupError("ASSISTANT_CONVERSATION_NOT_FOUND")
        now = datetime.now(UTC)
        sequence = conversation.last_message_sequence + 1
        event_sequence = conversation.last_event_sequence + 1
        message_id = uuid4()
        self._session.add(
            AssistantMessageModel(
                id=message_id,
                organization_id=job.organization_id,
                conversation_id=job.conversation_id,
                sequence=sequence,
                role=MessageRole.ASSISTANT.value,
                content_blocks=list(blocks),
                created_by_membership_id=None,
                turn_id=job.turn_id,
                dedupe_key=dedupe_key,
                created_at=now,
            )
        )
        self._session.add(
            AssistantEventModel(
                id=uuid4(),
                organization_id=job.organization_id,
                conversation_id=job.conversation_id,
                sequence=event_sequence,
                event_type="assistant.turn.response.v1",
                public_payload={
                    "turn_id": str(job.turn_id),
                    "orchestration_run_id": str(job.orchestration_run_id),
                    "message_id": str(message_id),
                    "message_sequence": sequence,
                    "status": "RESPONSE_AVAILABLE",
                },
                turn_id=job.turn_id,
                orchestration_run_id=job.orchestration_run_id,
                source_type=None,
                source_id=None,
                source_sequence=None,
                dedupe_key=f"event:{dedupe_key}",
                occurred_at=now,
            )
        )
        conversation.last_message_sequence = sequence
        conversation.last_event_sequence = event_sequence
        conversation.version += 1
        conversation.updated_at = now
        await self._session.flush()

    async def get_tool_invocation(
        self, *, organization_id: UUID, agent_run_id: UUID, dedupe_key: str
    ) -> ToolInvocation | None:
        await assert_context_live(self._session, organization_id, "agent_runs", agent_run_id)
        model = await self._session.scalar(
            select(ToolInvocationModel).where(
                ToolInvocationModel.organization_id == organization_id,
                ToolInvocationModel.agent_run_id == agent_run_id,
                ToolInvocationModel.dedupe_key == dedupe_key,
            )
        )
        return _tool_invocation(model) if model is not None else None

    async def append_tool_invocation(self, *, invocation: ToolInvocation) -> None:
        self._session.add(
            ToolInvocationModel(
                id=invocation.id,
                organization_id=invocation.organization_id,
                agent_run_id=invocation.agent_run_id,
                tool_id=invocation.tool_id,
                tool_version=invocation.tool_version,
                risk_level=invocation.risk_level,
                typed_input=invocation.typed_input,
                typed_output=invocation.typed_output,
                context_references=list(invocation.context_references),
                status=invocation.status.value,
                idempotency_key=invocation.idempotency_key,
                dedupe_key=invocation.dedupe_key,
                safe_error_code=invocation.safe_error_code,
                created_at=invocation.created_at,
                completed_at=invocation.completed_at,
            )
        )
        await self._session.flush()

    async def finish_tool_invocation(
        self,
        *,
        organization_id: UUID,
        invocation_id: UUID,
        status: str,
        typed_output: dict[str, object],
        context_references: tuple[dict[str, object], ...],
        safe_error_code: str | None,
    ) -> None:
        if status not in {"SUCCEEDED", "REJECTED", "FAILED"}:
            raise AssistantDomainLookupError("TOOL_INVOCATION_STATUS_INVALID")
        model = await self._session.scalar(
            select(ToolInvocationModel)
            .where(
                ToolInvocationModel.organization_id == organization_id,
                ToolInvocationModel.id == invocation_id,
                ToolInvocationModel.status == InvocationStatus.RUNNING.value,
            )
            .with_for_update()
        )
        if model is None:
            raise AssistantDomainLookupError("TOOL_INVOCATION_NOT_RUNNING")
        model.typed_output = typed_output
        model.context_references = list(context_references)
        model.status = status
        model.safe_error_code = safe_error_code
        model.completed_at = datetime.now(UTC)
        await self._session.flush()

    async def append_agent_model_invocation(self, *, invocation: AgentModelInvocation) -> None:
        existing = await self._session.scalar(
            select(AgentModelInvocationModel.id).where(
                AgentModelInvocationModel.organization_id == invocation.organization_id,
                AgentModelInvocationModel.agent_run_id == invocation.agent_run_id,
                AgentModelInvocationModel.invocation_key == invocation.invocation_key,
            )
        )
        if existing is not None:
            return
        self._session.add(
            AgentModelInvocationModel(
                id=invocation.id,
                organization_id=invocation.organization_id,
                agent_run_id=invocation.agent_run_id,
                provider=invocation.provider,
                model=invocation.model,
                prompt_version=invocation.prompt_version,
                schema_version=invocation.schema_version,
                invocation_key=invocation.invocation_key,
                status=invocation.status.value,
                input_tokens=invocation.input_tokens,
                output_tokens=invocation.output_tokens,
                duration_ms=invocation.duration_ms,
                safe_error_code=invocation.safe_error_code,
                created_at=invocation.created_at,
            )
        )
        await self._session.flush()

    async def append_event(self, *, event: AssistantEvent) -> AssistantEvent:
        conversation = await self._session.scalar(
            select(AssistantConversationModel)
            .where(
                AssistantConversationModel.organization_id == event.organization_id,
                AssistantConversationModel.id == event.conversation_id,
            )
            .with_for_update()
        )
        if conversation is None or event.sequence != conversation.last_event_sequence + 1:
            raise AssistantDomainLookupError("ASSISTANT_EVENT_SEQUENCE_INVALID")
        self._session.add(
            AssistantEventModel(
                id=event.id,
                organization_id=event.organization_id,
                conversation_id=event.conversation_id,
                sequence=event.sequence,
                event_type=event.event_type,
                public_payload=event.public_payload,
                turn_id=event.turn_id,
                orchestration_run_id=event.orchestration_run_id,
                agent_run_id=event.agent_run_id,
                source_type=event.source_type,
                source_id=event.source_id,
                source_sequence=event.source_sequence,
                dedupe_key=event.dedupe_key,
                occurred_at=event.occurred_at,
            )
        )
        conversation.last_event_sequence = event.sequence
        conversation.version += 1
        conversation.updated_at = event.occurred_at
        await self._session.flush()
        return event

    async def complete_job(self, *, job_id: UUID, worker_id: str) -> None:
        await self._session.execute(
            update(AssistantJobModel)
            .where(
                AssistantJobModel.id == job_id,
                AssistantJobModel.status == AssistantJobStatus.RUNNING.value,
                AssistantJobModel.locked_by == worker_id,
            )
            .values(
                status=AssistantJobStatus.COMPLETED.value,
                locked_by=None,
                lease_until=None,
                updated_at=datetime.now(UTC),
            )
        )

    async def fail_job(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        error_code: str,
        next_available_at: datetime,
    ) -> None:
        model = await self._session.scalar(
            select(AssistantJobModel)
            .where(
                AssistantJobModel.id == job_id,
                AssistantJobModel.status == AssistantJobStatus.RUNNING.value,
                AssistantJobModel.locked_by == worker_id,
            )
            .with_for_update()
        )
        if model is None:
            raise AssistantDomainLookupError("ASSISTANT_JOB_LEASE_INVALID")
        terminal = model.attempt_count >= model.max_attempts
        model.status = (
            AssistantJobStatus.FAILED.value if terminal else AssistantJobStatus.QUEUED.value
        )
        model.safe_error_code = error_code
        model.available_at = next_available_at
        model.locked_by = None
        model.lease_until = None
        now = datetime.now(UTC)
        model.updated_at = now
        if terminal and model.job_type == "assistant.turn.execute":
            job = _job(model)
            await self._session.execute(
                update(AssistantTurnModel)
                .where(
                    AssistantTurnModel.organization_id == model.organization_id,
                    AssistantTurnModel.id == model.turn_id,
                    AssistantTurnModel.status.in_(
                        (
                            AssistantTurnStatus.QUEUED.value,
                            AssistantTurnStatus.RUNNING.value,
                        )
                    ),
                )
                .values(
                    status=AssistantTurnStatus.FAILED.value,
                    safe_error_code=error_code,
                    updated_at=now,
                )
            )
            await self._session.execute(
                update(OrchestrationRunModel)
                .where(
                    OrchestrationRunModel.organization_id == model.organization_id,
                    OrchestrationRunModel.id == model.orchestration_run_id,
                    OrchestrationRunModel.status.in_(
                        (
                            OrchestrationRunStatus.QUEUED.value,
                            OrchestrationRunStatus.RUNNING.value,
                        )
                    ),
                )
                .values(
                    status=OrchestrationRunStatus.FAILED.value,
                    stop_reason="JOB_ATTEMPTS_EXHAUSTED",
                    safe_error_code=error_code,
                    completed_at=now,
                    updated_at=now,
                )
            )
            await self._session.execute(
                update(AgentRunModel)
                .where(
                    AgentRunModel.organization_id == model.organization_id,
                    AgentRunModel.orchestration_run_id == model.orchestration_run_id,
                    AgentRunModel.status.in_(
                        (AgentRunStatus.QUEUED.value, AgentRunStatus.RUNNING.value)
                    ),
                )
                .values(
                    status=AgentRunStatus.FAILED.value,
                    stop_reason="JOB_ATTEMPTS_EXHAUSTED",
                    safe_error_code=error_code,
                    completed_at=now,
                    updated_at=now,
                )
            )
            await self.append_assistant_blocks(
                job=job,
                blocks=(
                    {
                        "kind": "safe_error",
                        "code": error_code,
                        "message_key": "assistant.manual_fallback",
                    },
                ),
                dedupe_key=f"assistant:{model.turn_id}:manual-fallback",
            )
        await self._session.flush()


class AssistantDomainLookupError(ValueError):
    """Safe non-disclosing repository lookup failure."""
