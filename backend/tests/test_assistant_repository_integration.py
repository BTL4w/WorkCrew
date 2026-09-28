"""PostgreSQL Assistant repository isolation, dedupe and queue tests."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.core.database import create_database_engine, create_session_factory
from app.modules.assistant.adapters.database_models import (
    AgentCheckpointModel,
    AgentHandoffModel,
    AgentRunModel,
    AssistantEventModel,
    AssistantJobModel,
    AssistantMessageModel,
    AssistantTurnModel,
    OrchestrationRunModel,
)
from app.modules.assistant.adapters.title_gateway import DisabledTitleGateway
from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory
from app.modules.assistant.application.service import AssistantService, ResourceNotFoundError
from app.modules.assistant.application.title_service import ConversationTitleService
from app.modules.assistant.domain.models import (
    AgentCheckpoint,
    AgentHandoffRecord,
    AgentRun,
    AssistantConversation,
    AssistantEvent,
    AssistantJob,
    AssistantMessage,
    AssistantTurn,
    MessageRole,
    OrchestrationRun,
)
from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.planning_runs.adapters.database_models import OutboxEventModel
from app.modules.work.adapters.database_models import IdempotencyRecordModel
from work_management_ai.model_gateway.mock import MockModelGateway

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


def _actor(org: UUID, membership: UUID) -> AuthenticatedActor:
    return AuthenticatedActor(
        user_id=uuid4(),
        email=f"{membership.hex}@example.test",
        display_name="Assistant tester",
        membership_id=membership,
        organization_id=org,
        organization_name="Assistant test",
        role=MembershipRole.MANAGER,
    )


class _NoPlanningSnapshot:
    async def get_proposal_version(
        self, *, actor: AuthenticatedActor, proposal_id: UUID
    ) -> int | None:
        return None


async def _seed_members(connection: object, pairs: tuple[tuple[UUID, UUID], ...]) -> None:
    for organization_id, membership_id in pairs:
        user_id = uuid4()
        await connection.execute(  # type: ignore[attr-defined]
            text("INSERT INTO organizations (id, slug, name) VALUES (:id, :slug, 'Assistant')"),
            {"id": organization_id, "slug": f"assistant-{organization_id.hex}"},
        )
        await connection.execute(  # type: ignore[attr-defined]
            text(
                "INSERT INTO users "
                "(id, email_normalized, email_display, display_name, password_hash) "
                "VALUES (:id, :email, :email, 'Assistant', 'hash')"
            ),
            {"id": user_id, "email": f"{user_id.hex}@example.test"},
        )
        await connection.execute(  # type: ignore[attr-defined]
            text(
                "INSERT INTO memberships (id, organization_id, user_id, role) "
                "VALUES (:id, :org, :user, 'MANAGER')"
            ),
            {"id": membership_id, "org": organization_id, "user": user_id},
        )


def _turn_graph(
    *, conversation: AssistantConversation, actor: AuthenticatedActor
) -> tuple[AssistantMessage, AssistantTurn, OrchestrationRun, AssistantJob, AssistantEvent]:
    message_id, turn_id, run_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    message = AssistantMessage(
        id=message_id,
        organization_id=actor.organization_id,
        conversation_id=conversation.id,
        sequence=1,
        role=MessageRole.USER,
        content_blocks=({"kind": "text", "text": "Plan a customer event"},),
        created_by_membership_id=actor.membership_id,
        turn_id=turn_id,
        dedupe_key="message-1",
        created_at=now,
    )
    turn = AssistantTurn.create(
        id=turn_id,
        organization_id=actor.organization_id,
        conversation_id=conversation.id,
        user_message_id=message_id,
        actor_membership_id=actor.membership_id,
        objective="Plan a customer event",
        locale="en",
        now=now,
    )
    run = OrchestrationRun.create(
        id=run_id,
        organization_id=actor.organization_id,
        turn_id=turn_id,
        orchestrator_version="1.0.0",
        orchestrator_fingerprint="manifest-sha",
        execution_plan={"steps": []},
        budget={"max_iterations": 8},
        now=now,
    )
    job = AssistantJob.create(
        organization_id=actor.organization_id,
        conversation_id=conversation.id,
        turn_id=turn_id,
        orchestration_run_id=run_id,
        requester_membership_id=actor.membership_id,
        payload={"turn_id": str(turn_id)},
        now=now,
    )
    event = AssistantEvent(
        id=uuid4(),
        organization_id=actor.organization_id,
        conversation_id=conversation.id,
        sequence=1,
        event_type="assistant.turn.queued.v1",
        public_payload={"turn_id": str(turn_id), "status": "QUEUED"},
        turn_id=turn_id,
        orchestration_run_id=run_id,
        dedupe_key="turn-queued-1",
        occurred_at=now,
    )
    return message, turn, run, job, event


@pytest.mark.asyncio
async def test_owner_snapshot_isolated_and_conversation_creation_replays() -> None:
    engine = create_database_engine(Settings(environment="test"))
    organization_id, owner_id, other_id, foreign_org, foreign_member = (uuid4() for _ in range(5))
    owner, other, foreign = (
        _actor(organization_id, owner_id),
        _actor(organization_id, other_id),
        _actor(foreign_org, foreign_member),
    )
    try:
        async with engine.begin() as connection:
            await _seed_members(
                connection,
                ((organization_id, owner_id), (foreign_org, foreign_member)),
            )
            other_user = uuid4()
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, email_normalized, email_display, display_name, password_hash) "
                    "VALUES (:id, :email, :email, 'Other', 'hash')"
                ),
                {"id": other_user, "email": f"{other_user.hex}@example.test"},
            )
            await connection.execute(
                text(
                    "INSERT INTO memberships (id, organization_id, user_id, role) "
                    "VALUES (:id, :org, :user, 'MANAGER')"
                ),
                {"id": other_id, "org": organization_id, "user": other_user},
            )

        factory = PostgreSQLAssistantTransactionFactory(create_session_factory(engine))
        conversation = AssistantConversation.create(
            organization_id=organization_id,
            owner_membership_id=owner_id,
            locale="vi",
            title="Kế hoạch sự kiện",
        )
        async with factory(owner) as transaction:
            created = await transaction.repository.create_conversation_mutation(
                actor=owner,
                conversation=conversation,
                request_id="req-create-1",
                idempotency_key="conversation-key",
                request_fingerprint="fingerprint-a",
            )
            await transaction.commit()
        async with factory(owner) as transaction:
            replay = await transaction.repository.create_conversation_mutation(
                actor=owner,
                conversation=conversation,
                request_id="req-create-2",
                idempotency_key="conversation-key",
                request_fingerprint="fingerprint-a",
            )
            await transaction.commit()

        assert created.replayed is False
        assert replay.replayed is True
        assert replay.conversation.id == conversation.id
        async with factory(owner) as transaction:
            assert (
                await transaction.repository.get_conversation_snapshot(
                    actor=owner, conversation_id=conversation.id
                )
                is not None
            )
        async with factory(other) as transaction:
            assert (
                await transaction.repository.get_conversation_snapshot(
                    actor=other, conversation_id=conversation.id
                )
                is None
            )
        async with factory(foreign) as transaction:
            assert (
                await transaction.repository.get_conversation_snapshot(
                    actor=foreign, conversation_id=conversation.id
                )
                is None
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_message_turn_job_and_event_are_atomic_and_retry_safe() -> None:
    engine = create_database_engine(Settings(environment="test"))
    organization_id, membership_id = uuid4(), uuid4()
    actor = _actor(organization_id, membership_id)
    try:
        async with engine.begin() as connection:
            await _seed_members(connection, ((organization_id, membership_id),))
        factory = PostgreSQLAssistantTransactionFactory(create_session_factory(engine))
        conversation = AssistantConversation.create(
            organization_id=organization_id, owner_membership_id=membership_id, locale="en"
        )
        async with factory(actor) as transaction:
            await transaction.repository.create_conversation_mutation(
                actor=actor,
                conversation=conversation,
                request_id="req-create",
                idempotency_key="create-key",
                request_fingerprint="create-fingerprint",
            )
            await transaction.commit()
        message, turn, run, job, event = _turn_graph(conversation=conversation, actor=actor)
        async with factory(actor) as transaction:
            first = await transaction.repository.submit_message_mutation(
                actor=actor,
                message=message,
                turn=turn,
                run=run,
                job=job,
                event=event,
                request_id="req-message-1",
                idempotency_key="message-key",
                request_fingerprint="message-fingerprint",
            )
            await transaction.commit()
        async with factory(actor) as transaction:
            replay = await transaction.repository.submit_message_mutation(
                actor=actor,
                message=message,
                turn=turn,
                run=run,
                job=job,
                event=event,
                request_id="req-message-2",
                idempotency_key="message-key",
                request_fingerprint="message-fingerprint",
            )
            await transaction.commit()

        assert first.replayed is False
        assert replay.replayed is True
        assert replay.turn.id == turn.id
        async with factory(actor) as transaction:
            session = transaction.session
            assert (
                await session.scalar(select(func.count()).select_from(AssistantMessageModel)) == 1
            )
            assert await session.scalar(select(func.count()).select_from(AssistantEventModel)) == 1
            assert await session.scalar(select(func.count()).select_from(AssistantJobModel)) == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_private_card_action_is_unwrapped_for_planning_agent() -> None:
    engine = create_database_engine(Settings(environment="test"))
    organization_id, membership_id = uuid4(), uuid4()
    actor = _actor(organization_id, membership_id)
    try:
        async with engine.begin() as connection:
            await _seed_members(connection, ((organization_id, membership_id),))
        factory = PostgreSQLAssistantTransactionFactory(create_session_factory(engine))
        conversation = AssistantConversation.create(
            organization_id=organization_id,
            owner_membership_id=membership_id,
            locale="vi",
        )
        async with factory(actor) as transaction:
            await transaction.repository.create_conversation_mutation(
                actor=actor,
                conversation=conversation,
                request_id="request-create-action",
                idempotency_key="conversation-action-key",
                request_fingerprint="conversation-action-fingerprint",
            )
            await transaction.commit()
        message, turn, run, job, event = _turn_graph(conversation=conversation, actor=actor)
        proposal_id = uuid4()
        workflow_run_id = uuid4()
        action = {
            "kind": "PLANNING_REVISE",
            "workflow_run_id": str(workflow_run_id),
            "proposal_id": str(proposal_id),
            "expected_version": 1,
        }
        message = replace(
            message,
            content_blocks=(
                {"kind": "text", "text": "Mở rộng đến cuối tháng 11"},
                {"kind": "accepted_card_action", "action": action},
            ),
        )
        async with factory(actor) as transaction:
            await transaction.repository.submit_message_mutation(
                actor=actor,
                message=message,
                turn=turn,
                run=run,
                job=job,
                event=event,
                request_id="request-submit-action",
                idempotency_key="message-action-key",
                request_fingerprint="message-action-fingerprint",
            )
            await transaction.commit()

        async with factory(actor) as transaction:
            accepted = await transaction.repository.get_accepted_planning_action(
                organization_id=organization_id,
                turn_id=turn.id,
            )

        assert accepted == action
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_submit_failure_after_ordered_flush_rolls_back_the_entire_graph() -> None:
    engine = create_database_engine(Settings(environment="test"))
    organization_id, membership_id = uuid4(), uuid4()
    actor = _actor(organization_id, membership_id)
    try:
        async with engine.begin() as connection:
            await _seed_members(connection, ((organization_id, membership_id),))
        factory = PostgreSQLAssistantTransactionFactory(create_session_factory(engine))
        conversation = AssistantConversation.create(
            organization_id=organization_id, owner_membership_id=membership_id, locale="en"
        )
        async with factory(actor) as transaction:
            await transaction.repository.create_conversation_mutation(
                actor=actor,
                conversation=conversation,
                request_id="req-create",
                idempotency_key="create-key",
                request_fingerprint="create-fingerprint",
            )
            await transaction.commit()

        message, turn, run, job, event = _turn_graph(conversation=conversation, actor=actor)
        invalid_event = AssistantEvent(
            id=event.id,
            organization_id=event.organization_id,
            conversation_id=event.conversation_id,
            sequence=event.sequence,
            event_type=event.event_type,
            public_payload=event.public_payload,
            turn_id=event.turn_id,
            orchestration_run_id=event.orchestration_run_id,
            agent_run_id=uuid4(),
            dedupe_key=event.dedupe_key,
            occurred_at=event.occurred_at,
        )
        with pytest.raises(DBAPIError):
            async with factory(actor) as transaction:
                await transaction.repository.submit_message_mutation(
                    actor=actor,
                    message=message,
                    turn=turn,
                    run=run,
                    job=job,
                    event=invalid_event,
                    request_id="req-message-fails",
                    idempotency_key="message-key-fails",
                    request_fingerprint="message-fingerprint-fails",
                )

        async with factory(actor) as transaction:
            session = transaction.session
            assert (
                await session.scalar(select(func.count()).select_from(AssistantMessageModel)) == 0
            )
            assert await session.scalar(select(func.count()).select_from(AssistantTurnModel)) == 0
            assert (
                await session.scalar(select(func.count()).select_from(OrchestrationRunModel)) == 0
            )
            assert await session.scalar(select(func.count()).select_from(AssistantJobModel)) == 0
            assert await session.scalar(select(func.count()).select_from(AssistantEventModel)) == 0
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AuditEventModel)
                    .where(AuditEventModel.action == "assistant.message.submitted")
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(IdempotencyRecordModel)
                    .where(IdempotencyRecordModel.idempotency_key == "message-key-fails")
                )
                == 0
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_claim_is_tenant_scoped_and_retry_lease_safe() -> None:
    engine = create_database_engine(Settings(environment="test"))
    organization_id, membership_id = uuid4(), uuid4()
    actor = _actor(organization_id, membership_id)
    try:
        async with engine.begin() as connection:
            await _seed_members(connection, ((organization_id, membership_id),))
        factory = PostgreSQLAssistantTransactionFactory(create_session_factory(engine))
        conversation = AssistantConversation.create(
            organization_id=organization_id, owner_membership_id=membership_id, locale="en"
        )
        async with factory(actor) as transaction:
            await transaction.repository.create_conversation_mutation(
                actor=actor,
                conversation=conversation,
                request_id="req-create",
                idempotency_key="create-key",
                request_fingerprint="create-fingerprint",
            )
            await transaction.commit()
        message, turn, run, job, event = _turn_graph(conversation=conversation, actor=actor)
        async with factory(actor) as transaction:
            await transaction.repository.submit_message_mutation(
                actor=actor,
                message=message,
                turn=turn,
                run=run,
                job=job,
                event=event,
                request_id="req-message",
                idempotency_key="message-key",
                request_fingerprint="message-fingerprint",
            )
            await transaction.commit()

        now = datetime.now(UTC)
        async with factory(organization_id) as transaction:
            claimed = await transaction.repository.claim_job(
                organization_id=organization_id,
                worker_id="assistant-worker-1",
                now=now,
                lease_until=now + timedelta(minutes=1),
            )
            await transaction.commit()
        assert claimed is not None
        assert claimed.id == job.id
        assert claimed.attempt_count == 1

        agent_run = AgentRun.create(
            organization_id=organization_id,
            orchestration_run_id=run.id,
            agent_id="work_management_orchestrator",
            agent_version="1.0.0",
            manifest_fingerprint="orchestrator-manifest-sha",
            capability="route_assistant_turn",
            typed_input={"turn_id": str(turn.id)},
            budget={"max_iterations": 8},
            now=now,
        )
        handoff = AgentHandoffRecord(
            id=uuid4(),
            organization_id=organization_id,
            orchestration_run_id=run.id,
            parent_agent_run_id=agent_run.id,
            target_agent_id="planning_specialist",
            target_agent_version="1.0.0",
            capability="create_plan_proposal",
            objective="Create a bounded project plan",
            typed_input={"objective": "Plan a customer event"},
            context_references=(),
            budget={"max_iterations": 4},
            step_id="plan-step-1",
            idempotency_key="handoff-key-1",
            dedupe_key="handoff-dedupe-1",
            created_at=now,
        )
        checkpoint = AgentCheckpoint(
            id=uuid4(),
            organization_id=organization_id,
            orchestration_run_id=run.id,
            agent_run_id=agent_run.id,
            sequence=1,
            node="route_intent",
            typed_state={"intent": "planning"},
            checkpoint_version="1.0.0",
            created_at=now,
        )
        progress_event = AssistantEvent(
            id=uuid4(),
            organization_id=organization_id,
            conversation_id=conversation.id,
            sequence=2,
            event_type="assistant.agent.started.v1",
            public_payload={"agent": "planning_specialist"},
            turn_id=turn.id,
            orchestration_run_id=run.id,
            agent_run_id=agent_run.id,
            dedupe_key="agent-started-1",
            occurred_at=now,
        )
        async with factory(organization_id) as transaction:
            started = await transaction.repository.begin_orchestration(job=claimed)
            await transaction.repository.append_agent_run(run=agent_run)
            await transaction.repository.append_handoff(handoff=handoff)
            await transaction.repository.save_checkpoint(checkpoint=checkpoint)
            await transaction.repository.append_event(event=progress_event)
            await transaction.repository.complete_job(
                job_id=claimed.id, worker_id="assistant-worker-1"
            )
            await transaction.commit()

        assert started.status.value == "RUNNING"
        async with factory(organization_id) as transaction:
            session = transaction.session
            assert await session.scalar(select(func.count()).select_from(AgentRunModel)) == 1
            assert await session.scalar(select(func.count()).select_from(AgentHandoffModel)) == 1
            assert await session.scalar(select(func.count()).select_from(AgentCheckpointModel)) == 1
            assert await session.scalar(select(func.count()).select_from(AssistantEventModel)) == 2
        async with factory(uuid4()) as transaction:
            assert (
                await transaction.repository.claim_job(
                    organization_id=uuid4(),
                    worker_id="assistant-worker-foreign",
                    now=now,
                    lease_until=now + timedelta(minutes=1),
                )
                is None
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_service_persists_two_ordered_turns_and_lists_only_owner_data() -> None:
    engine = create_database_engine(Settings(environment="test"))
    organization_id, owner_id, other_id = uuid4(), uuid4(), uuid4()
    owner = _actor(organization_id, owner_id)
    other = _actor(organization_id, other_id)
    try:
        async with engine.begin() as connection:
            await _seed_members(connection, ((organization_id, owner_id),))
            other_user = uuid4()
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, email_normalized, email_display, display_name, password_hash) "
                    "VALUES (:id, :email, :email, 'Other', 'hash')"
                ),
                {"id": other_user, "email": f"{other_user.hex}@example.test"},
            )
            await connection.execute(
                text(
                    "INSERT INTO memberships (id, organization_id, user_id, role) "
                    "VALUES (:id, :org, :user, 'EMPLOYEE')"
                ),
                {"id": other_id, "org": organization_id, "user": other_user},
            )
        factory = PostgreSQLAssistantTransactionFactory(create_session_factory(engine))
        service = AssistantService(
            transaction_factory=factory,
            planning_snapshot=_NoPlanningSnapshot(),
            orchestrator_version="1.0.0",
            orchestrator_fingerprint="orchestrator-manifest-fingerprint",
        )
        created = await service.create_conversation(
            actor=owner,
            locale="en",
            title="Ordered transcript",
            request_id="request-create-ordered",
            idempotency_key="conversation-ordered-key",
        )
        first = await service.post_message(
            actor=owner,
            conversation_id=created.conversation.id,
            message="First message",
            locale="en",
            card_action=None,
            if_match_version=None,
            request_id="request-first-ordered",
            idempotency_key="message-ordered-key-1",
        )
        second = await service.post_message(
            actor=owner,
            conversation_id=created.conversation.id,
            message="Second message",
            locale="en",
            card_action=None,
            if_match_version=None,
            request_id="request-second-ordered",
            idempotency_key="message-ordered-key-2",
        )

        assert (first.message.sequence, second.message.sequence) == (1, 2)
        assert (first.event.sequence, second.event.sequence) == (1, 2)
        assert [item.id for item in await service.list_conversations(actor=owner)] == [
            created.conversation.id
        ]
        assert await service.list_conversations(actor=other) == []
        with pytest.raises(ResourceNotFoundError):
            await service.post_message(
                actor=other,
                conversation_id=created.conversation.id,
                message="Try another owner's conversation",
                locale="en",
                card_action=None,
                if_match_version=None,
                request_id="request-other-owner-rejected",
                idempotency_key="message-other-owner-key",
            )
        async with factory(other) as transaction:
            rejected_audits = await transaction.session.scalars(
                select(AuditEventModel).where(
                    AuditEventModel.action == "assistant.message.submit",
                    AuditEventModel.resource_id == created.conversation.id,
                )
            )
            audit = rejected_audits.one()
            assert audit.reason_data == {"code": "RESOURCE_NOT_FOUND"}
            assert "Try another" not in str(audit.reason_data)
        snapshot = await service.get_conversation(
            actor=owner,
            conversation_id=created.conversation.id,
        )
        assert [message.sequence for message in snapshot.messages] == [1, 2]
        assert [event.sequence for event in snapshot.events] == [1, 2]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_first_message_enqueues_one_title_job_and_replay_returns_turn_job() -> None:
    engine = create_database_engine(Settings(environment="test"))
    org, member = uuid4(), uuid4()
    actor = _actor(org, member)
    try:
        async with engine.begin() as connection:
            await _seed_members(connection, ((org, member),))
        factory = PostgreSQLAssistantTransactionFactory(create_session_factory(engine))
        service = AssistantService(
            transaction_factory=factory,
            planning_snapshot=_NoPlanningSnapshot(),
            orchestrator_version="1.0.0",
            orchestrator_fingerprint="test",
        )
        created = await service.create_conversation(
            actor=actor, locale="en", title=None, request_id="create", idempotency_key="create"
        )
        kwargs = dict(
            actor=actor,
            conversation_id=created.conversation.id,
            message="Plan a customer event",
            locale="en",
            card_action=None,
            if_match_version=None,
            request_id="first",
            idempotency_key="first",
        )
        first = await service.post_message(**kwargs)  # type: ignore[arg-type]
        replay = await service.post_message(**kwargs)  # type: ignore[arg-type]
        await service.post_message(
            actor=actor,
            conversation_id=created.conversation.id,
            message="Another topic",
            locale="en",
            card_action=None,
            if_match_version=None,
            request_id="second",
            idempotency_key="second",
        )
        assert replay.replayed and replay.job.id == first.job.id
        async with factory(actor) as transaction:
            jobs = (await transaction.session.scalars(select(AssistantJobModel))).all()
            assert sorted(job.job_type for job in jobs) == [
                "assistant.conversation.title",
                "assistant.turn.execute",
                "assistant.turn.execute",
            ]
            title_job = next(job for job in jobs if job.job_type == "assistant.conversation.title")
            assert title_job.turn_id == first.turn.id
            assert "message" not in title_job.payload
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_disabled", [False, True])
async def test_title_job_is_owner_scoped_atomic_and_replay_safe(provider_disabled: bool) -> None:
    engine = create_database_engine(Settings(environment="test"))
    org, member, foreign_org, foreign_member = (uuid4() for _ in range(4))
    actor, foreign = _actor(org, member), _actor(foreign_org, foreign_member)
    try:
        async with engine.begin() as connection:
            await _seed_members(connection, ((org, member), (foreign_org, foreign_member)))
        factory = PostgreSQLAssistantTransactionFactory(create_session_factory(engine))
        service = AssistantService(
            transaction_factory=factory,
            planning_snapshot=_NoPlanningSnapshot(),
            orchestrator_version="1.0.0",
            orchestrator_fingerprint="test",
        )
        created = await service.create_conversation(
            actor=actor, locale="vi", title=None, request_id="create", idempotency_key="create"
        )
        first = await service.post_message(
            actor=actor,
            conversation_id=created.conversation.id,
            message="Lập kế hoạch ra mắt sản phẩm",
            locale="vi",
            card_action=None,
            if_match_version=None,
            request_id="first",
            idempotency_key="first",
        )
        async with factory(org) as transaction:
            now = datetime.now(UTC)
            job = await transaction.repository.claim_job(
                organization_id=org,
                worker_id="title-worker",
                now=now,
                lease_until=now + timedelta(seconds=60),
                job_type="assistant.conversation.title",
            )
            await transaction.commit()
        assert job is not None and job.job_type == "assistant.conversation.title"
        assert job.turn_id == first.turn.id
        gateway = (
            DisabledTitleGateway()
            if provider_disabled
            else MockModelGateway(
                fixtures={"conversation_title.vi.v1": {"title": "Kế hoạch ra mắt sản phẩm"}},
                model_ref="mock:title-v1",
            )
        )
        naming = ConversationTitleService(transaction_factory=factory, gateway=gateway)
        # Forged tenant/owner cannot read or mutate either at service or repository level.
        await naming.execute_job(job=job, actor=foreign)
        other = replace(actor, membership_id=uuid4())
        await naming.execute_job(job=job, actor=other)
        async with factory(foreign) as transaction:
            assert not await transaction.repository.set_conversation_title(
                actor=foreign, job=job, title="Foreign title", metadata={}
            )
            assert not await transaction.session.scalar(select(AssistantMessageModel.id))
        await naming.execute_job(job=job, actor=actor)
        await naming.execute_job(job=job, actor=actor)
        snapshot = await service.get_conversation(
            actor=actor, conversation_id=created.conversation.id
        )
        expected = (
            "Lập kế hoạch ra mắt sản phẩm" if provider_disabled else "Kế hoạch ra mắt sản phẩm"
        )
        assert snapshot.conversation.title == expected
        assert len(snapshot.messages) == 1  # No extra assistant message or Agent run.
        assert len(snapshot.events) == 2
        assert snapshot.events[-1].event_type == "assistant.conversation.titled.v1"
        assert snapshot.turns[0].status.value == "QUEUED"
        async with factory(actor) as transaction:
            audits = (
                await transaction.session.scalars(
                    select(AuditEventModel).where(
                        AuditEventModel.action == "assistant.conversation.titled"
                    )
                )
            ).all()
            assert len(audits) == 1
            assert audits[0].reason_data["fallback"] is provider_disabled
            assert audits[0].reason_data["prompt_version"] == "1.0.0"
            outbox = (
                await transaction.session.scalars(
                    select(OutboxEventModel).where(
                        OutboxEventModel.aggregate_id == created.conversation.id
                    )
                )
            ).all()
            assert len(outbox) == 1
            assert outbox[0].id == snapshot.events[-1].id
            assert outbox[0].event_id == snapshot.events[-1].id
            assert outbox[0].envelope_version == "1.0"
            assert not await transaction.repository.set_conversation_title(
                actor=actor, job=job, title="Overwritten", metadata={}
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_explicit_conversation_title_does_not_enqueue_naming() -> None:
    engine = create_database_engine(Settings(environment="test"))
    org, member = uuid4(), uuid4()
    actor = _actor(org, member)
    try:
        async with engine.begin() as connection:
            await _seed_members(connection, ((org, member),))
        factory = PostgreSQLAssistantTransactionFactory(create_session_factory(engine))
        service = AssistantService(
            transaction_factory=factory,
            planning_snapshot=_NoPlanningSnapshot(),
            orchestrator_version="1.0.0",
            orchestrator_fingerprint="test",
        )
        created = await service.create_conversation(
            actor=actor,
            locale="en",
            title="My custom name",
            request_id="create",
            idempotency_key="create",
        )
        await service.post_message(
            actor=actor,
            conversation_id=created.conversation.id,
            message="Name it differently",
            locale="en",
            card_action=None,
            if_match_version=None,
            request_id="first",
            idempotency_key="first",
        )
        async with factory(actor) as transaction:
            assert (
                await transaction.session.scalar(
                    select(func.count()).select_from(AssistantJobModel)
                )
                == 1
            )
        snapshot = await service.get_conversation(
            actor=actor, conversation_id=created.conversation.id
        )
        assert snapshot.conversation.title == "My custom name"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_exhausted_title_job_does_not_fail_or_add_messages_to_answer() -> None:
    engine = create_database_engine(Settings(environment="test"))
    org, member = uuid4(), uuid4()
    actor = _actor(org, member)
    try:
        async with engine.begin() as connection:
            await _seed_members(connection, ((org, member),))
        factory = PostgreSQLAssistantTransactionFactory(create_session_factory(engine))
        service = AssistantService(
            transaction_factory=factory,
            planning_snapshot=_NoPlanningSnapshot(),
            orchestrator_version="1.0.0",
            orchestrator_fingerprint="test",
        )
        created = await service.create_conversation(
            actor=actor, locale="en", title=None, request_id="create", idempotency_key="create"
        )
        first = await service.post_message(
            actor=actor,
            conversation_id=created.conversation.id,
            message="Plan a customer event",
            locale="en",
            card_action=None,
            if_match_version=None,
            request_id="first",
            idempotency_key="first",
        )
        for attempt in range(3):
            now = datetime.now(UTC)
            async with factory(org) as transaction:
                claimed = await transaction.repository.claim_job(
                    organization_id=org,
                    worker_id="title-worker",
                    now=now,
                    lease_until=now + timedelta(seconds=60),
                    job_type="assistant.conversation.title",
                )
                assert claimed is not None and claimed.attempt_count == attempt + 1
                assert claimed.job_type == "assistant.conversation.title"
                await transaction.repository.fail_job(
                    job_id=claimed.id,
                    worker_id="title-worker",
                    error_code="DATABASE_UNAVAILABLE",
                    next_available_at=now,
                )
                await transaction.commit()
        snapshot = await service.get_conversation(
            actor=actor, conversation_id=created.conversation.id
        )
        assert snapshot.turns[0].status.value == "QUEUED"
        assert snapshot.orchestration_runs[0].status.value == "QUEUED"
        assert len(snapshot.messages) == len(snapshot.events) == 1
        async with factory(org) as transaction:
            claimed = await transaction.repository.claim_job(
                organization_id=org,
                worker_id="answer-worker",
                now=datetime.now(UTC),
                lease_until=datetime.now(UTC) + timedelta(seconds=60),
            )
            assert claimed is not None and claimed.id == first.job.id
    finally:
        await engine.dispose()
