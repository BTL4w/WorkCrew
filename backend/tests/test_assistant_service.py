"""Task 6 service-level tests for AssistantService and AssistantEventService.

All tests use in-memory fakes — no model, provider or Agent Runtime is called.
"""

# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportUnknownVariableType=false, reportUnknownMemberType=false

from __future__ import annotations

import copy
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

import pytest

from app.modules.assistant.adapters.planning_snapshot import PostgreSQLPlanningSnapshot
from app.modules.assistant.application.ports import (
    AssistantConversationMutationResult,
    AssistantConversationSnapshot,
    AssistantTurnMutationResult,
)
from app.modules.assistant.application.service import (
    AssistantService,
    AssistantServiceError,
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from app.modules.assistant.domain.models import (
    AssistantConversation,
    AssistantEvent,
    AssistantIdempotencyKeyReusedError,
    AssistantJob,
    AssistantMessage,
    AssistantTurn,
    OrchestrationRun,
)
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _actor(role: MembershipRole = MembershipRole.MANAGER) -> AuthenticatedActor:
    return AuthenticatedActor(
        user_id=uuid4(),
        email="user@example.test",
        display_name="User",
        membership_id=uuid4(),
        organization_id=uuid4(),
        organization_name="Org",
        role=role,
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeRepository:
    """In-memory fake for AssistantRepository."""

    conversations: dict[UUID, AssistantConversation] = field(default_factory=dict)
    messages: list[AssistantMessage] = field(default_factory=list)
    turns: list[AssistantTurn] = field(default_factory=list)
    runs: list[OrchestrationRun] = field(default_factory=list)
    jobs: list[AssistantJob] = field(default_factory=list)
    events: list[AssistantEvent] = field(default_factory=list)
    idempotency: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    audit_log: list[dict[str, Any]] = field(default_factory=list)

    # Injection point for rollback test: raise after message insert
    fail_after_message: bool = False
    call_counts: dict[str, int] = field(default_factory=dict)

    def _bump(self, name: str) -> None:
        self.call_counts[name] = self.call_counts.get(name, 0) + 1

    async def create_conversation_mutation(
        self,
        *,
        actor: AuthenticatedActor,
        conversation: AssistantConversation,
        request_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> AssistantConversationMutationResult:
        self._bump("create_conversation")
        key = (str(actor.membership_id), "assistant.conversation.create", idempotency_key)
        if key in self.idempotency:
            stored = self.idempotency[key]
            if stored["fingerprint"] != request_fingerprint:
                raise AssistantIdempotencyKeyReusedError("IDEMPOTENCY_KEY_REUSED")
            existing = self.conversations[stored["conversation_id"]]
            return AssistantConversationMutationResult(conversation=existing, replayed=True)
        self.conversations[conversation.id] = conversation
        self.idempotency[key] = {
            "fingerprint": request_fingerprint,
            "conversation_id": conversation.id,
        }
        self.audit_log.append({"action": "conversation.created", "id": conversation.id})
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
        self._bump("submit_message")
        key = (
            str(actor.membership_id),
            f"assistant.message.submit:{message.conversation_id}",
            idempotency_key,
        )
        if key in self.idempotency:
            stored = self.idempotency[key]
            if stored["fingerprint"] != request_fingerprint:
                raise AssistantIdempotencyKeyReusedError("IDEMPOTENCY_KEY_REUSED")
            # Replay
            existing_msg = next(m for m in self.messages if m.id == stored["message_id"])
            existing_turn = next(t for t in self.turns if t.id == stored["turn_id"])
            existing_run = next(r for r in self.runs if r.id == stored["run_id"])
            existing_job = next(j for j in self.jobs if j.orchestration_run_id == existing_run.id)
            existing_event = next(e for e in self.events if e.turn_id == existing_turn.id)
            return AssistantTurnMutationResult(
                message=existing_msg,
                turn=existing_turn,
                run=existing_run,
                job=existing_job,
                event=existing_event,
                replayed=True,
            )
        # Validate conversation belongs to actor
        conv = self.conversations.get(message.conversation_id)
        if conv is None or conv.owner_membership_id != actor.membership_id:
            from app.modules.assistant.adapters.repository import AssistantDomainLookupError

            raise AssistantDomainLookupError("ASSISTANT_CONVERSATION_NOT_FOUND")

        message = replace(message, sequence=conv.last_message_sequence + 1)
        event = replace(event, sequence=conv.last_event_sequence + 1)
        self.messages.append(message)
        if self.fail_after_message:
            raise RuntimeError("simulated DB failure after message insert")
        self.turns.append(turn)
        self.runs.append(run)
        self.jobs.append(job)
        self.events.append(event)
        self.conversations[conv.id] = replace(
            conv,
            last_message_sequence=message.sequence,
            last_event_sequence=event.sequence,
            version=conv.version + 2,
        )
        self.idempotency[key] = {
            "fingerprint": request_fingerprint,
            "message_id": message.id,
            "turn_id": turn.id,
            "run_id": run.id,
        }
        self.audit_log.append({"action": "message.submitted", "turn_id": turn.id})
        return AssistantTurnMutationResult(
            message=message, turn=turn, run=run, job=job, event=event, replayed=False
        )

    async def get_conversation_snapshot(
        self, *, actor: AuthenticatedActor, conversation_id: UUID
    ) -> AssistantConversationSnapshot | None:
        conv = self.conversations.get(conversation_id)
        if conv is None or conv.owner_membership_id != actor.membership_id:
            return None
        msgs = [m for m in self.messages if m.conversation_id == conversation_id]
        turns = [t for t in self.turns if t.conversation_id == conversation_id]
        runs_list = [r for r in self.runs if r.turn_id in {t.id for t in turns}]
        evts = [e for e in self.events if e.conversation_id == conversation_id]
        return AssistantConversationSnapshot(
            conversation=conv,
            messages=tuple(msgs),
            turns=tuple(turns),
            orchestration_runs=tuple(runs_list),
            events=tuple(evts),
        )

    async def list_conversations(
        self, *, actor: AuthenticatedActor, limit: int
    ) -> list[AssistantConversation]:
        return [
            c
            for c in self.conversations.values()
            if c.owner_membership_id == actor.membership_id
            and c.organization_id == actor.organization_id
        ][:limit]

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
        self.audit_log.append(
            {
                "action": action,
                "outcome": "REJECTED",
                "resource_type": resource_type,
                "resource_id": resource_id,
                "request_id": request_id,
                "reason_code": reason_code,
            }
        )

    # Unused by Task 6 service but satisfy protocol
    async def claim_job(self, **_: Any) -> AssistantJob | None:
        return None

    async def begin_orchestration(self, **_: Any) -> OrchestrationRun:
        raise NotImplementedError

    async def append_agent_run(self, **_: Any) -> Any:
        raise NotImplementedError

    async def append_handoff(self, **_: Any) -> None:
        raise NotImplementedError

    async def save_checkpoint(self, **_: Any) -> None:
        raise NotImplementedError

    async def append_event(self, **_: Any) -> AssistantEvent:
        raise NotImplementedError

    async def complete_job(self, **_: Any) -> None:
        raise NotImplementedError

    async def fail_job(self, **_: Any) -> None:
        raise NotImplementedError


class FakeTransaction(AbstractAsyncContextManager["FakeTransaction"]):
    def __init__(
        self, repository: FakeRepository, *, safe_audit_repo: FakeRepository | None = None
    ) -> None:
        self._repo = repository
        self._safe_audit_repo = safe_audit_repo
        self.committed = False
        self.rolled_back = False
        self._snapshot: FakeRepository | None = None

    @property
    def repository(self) -> FakeRepository:
        return self._repo

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def __aenter__(self) -> Self:
        self._snapshot = copy.deepcopy(self._repo)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is not None and not self.committed:
            self.rolled_back = True
            assert self._snapshot is not None
            self._repo.__dict__.update(self._snapshot.__dict__)


class FakeTransactionFactory:
    def __init__(self, repository: FakeRepository) -> None:
        self._repo = repository
        self.transactions: list[FakeTransaction] = []

    def __call__(self, context: Any) -> FakeTransaction:
        txn = FakeTransaction(self._repo)
        self.transactions.append(txn)
        return txn


class NullPlanningSnapshot:
    """PlanningSnapshotPort that always returns None — no planning context."""

    async def get_proposal_version(
        self, *, actor: AuthenticatedActor, proposal_id: UUID
    ) -> int | None:
        return None


class FixedPlanningSnapshot:
    def __init__(self, version: int | None) -> None:
        self.version = version

    async def get_proposal_version(
        self, *, actor: AuthenticatedActor, proposal_id: UUID
    ) -> int | None:
        return self.version


class PlanningRepository:
    def __init__(self, proposal: Any) -> None:
        self.proposal = proposal

    async def get_proposal(self, **_: Any) -> Any:
        return self.proposal


class PlanningTransaction(AbstractAsyncContextManager["PlanningTransaction"]):
    def __init__(self, repository: PlanningRepository) -> None:
        self.repository = repository

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        return None


@pytest.mark.asyncio
async def test_planning_snapshot_uses_actor_scoped_planning_repository() -> None:
    actor = _actor(MembershipRole.MANAGER)
    proposal_id = uuid4()
    proposal = type("Proposal", (), {"current_version_number": 7})()
    transaction = PlanningTransaction(PlanningRepository(proposal))
    seen_context: list[AuthenticatedActor] = []

    def factory(context: AuthenticatedActor) -> PlanningTransaction:
        seen_context.append(context)
        return transaction

    adapter = PostgreSQLPlanningSnapshot(factory)  # type: ignore[arg-type]

    version = await adapter.get_proposal_version(actor=actor, proposal_id=proposal_id)

    assert version == 7
    assert seen_context == [actor]


def _service(repo: FakeRepository) -> AssistantService:
    return AssistantService(
        transaction_factory=FakeTransactionFactory(repo),
        planning_snapshot=NullPlanningSnapshot(),  # type: ignore[arg-type]
        orchestrator_version="1.0.0",
        orchestrator_fingerprint="orchestrator-manifest-fingerprint",
    )


# ---------------------------------------------------------------------------
# Tests: create_conversation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role", [MembershipRole.ADMIN, MembershipRole.MANAGER, MembershipRole.EMPLOYEE]
)
async def test_all_roles_can_create_conversation(role: MembershipRole) -> None:
    repo = FakeRepository()
    svc = _service(repo)
    actor = _actor(role)

    result = await svc.create_conversation(
        actor=actor,
        locale="vi",
        title=None,
        request_id="req-1",
        idempotency_key="ik-create-1",
    )

    assert result.conversation.locale == "vi"
    assert result.conversation.owner_membership_id == actor.membership_id
    assert result.conversation.organization_id == actor.organization_id
    assert not result.replayed
    assert len(repo.conversations) == 1


@pytest.mark.asyncio
async def test_create_conversation_same_key_same_payload_replays() -> None:
    repo = FakeRepository()
    svc = _service(repo)
    actor = _actor()

    r1 = await svc.create_conversation(
        actor=actor, locale="vi", title=None, request_id="req-1", idempotency_key="ik-c1"
    )
    r2 = await svc.create_conversation(
        actor=actor, locale="vi", title=None, request_id="req-2", idempotency_key="ik-c1"
    )

    assert r2.replayed
    assert r1.conversation.id == r2.conversation.id
    assert len(repo.conversations) == 1


@pytest.mark.asyncio
async def test_create_conversation_fingerprint_uses_normalized_title() -> None:
    repo = FakeRepository()
    svc = _service(repo)
    actor = _actor()

    first = await svc.create_conversation(
        actor=actor,
        locale="en",
        title="  Weekly planning  ",
        request_id="request-title-1",
        idempotency_key="conversation-title-key",
    )
    replay = await svc.create_conversation(
        actor=actor,
        locale="en",
        title="Weekly planning",
        request_id="request-title-2",
        idempotency_key="conversation-title-key",
    )

    assert replay.replayed is True
    assert replay.conversation.id == first.conversation.id


@pytest.mark.asyncio
async def test_create_conversation_same_key_different_payload_conflicts() -> None:
    repo = FakeRepository()
    svc = _service(repo)
    actor = _actor()

    await svc.create_conversation(
        actor=actor, locale="vi", title=None, request_id="req-1", idempotency_key="ik-c2"
    )
    with pytest.raises(IdempotencyConflictError):
        await svc.create_conversation(
            actor=actor, locale="en", title=None, request_id="req-2", idempotency_key="ik-c2"
        )


# ---------------------------------------------------------------------------
# Tests: list_conversations / get_conversation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_and_get_only_own_conversations() -> None:
    owner = _actor()
    other = _actor()
    repo = FakeRepository()
    svc = _service(repo)

    result = await svc.create_conversation(
        actor=owner, locale="vi", title=None, request_id="r1", idempotency_key="ik-o1"
    )
    cid = result.conversation.id

    listed = await svc.list_conversations(actor=owner, limit=20)
    assert len(listed) == 1 and listed[0].id == cid

    # other actor (different membership) sees nothing
    listed_other = await svc.list_conversations(actor=other, limit=20)
    assert listed_other == []

    # get own
    snap = await svc.get_conversation(actor=owner, conversation_id=cid)
    assert snap is not None and snap.conversation.id == cid

    # other actor non-disclosing 404
    with pytest.raises(ResourceNotFoundError):
        await svc.get_conversation(actor=other, conversation_id=cid)


@pytest.mark.asyncio
async def test_other_owner_and_foreign_tenant_are_non_disclosing() -> None:
    repo = FakeRepository()
    svc = _service(repo)
    owner = _actor()
    await svc.create_conversation(
        actor=owner, locale="en", title=None, request_id="r1", idempotency_key="ik-nd1"
    )
    [conv] = list(repo.conversations.values())

    # same tenant, different membership
    same_tenant_other = AuthenticatedActor(
        user_id=uuid4(),
        email="other@t.test",
        display_name="Other",
        membership_id=uuid4(),
        organization_id=owner.organization_id,  # same org
        organization_name="Org",
        role=MembershipRole.MANAGER,
    )
    with pytest.raises(ResourceNotFoundError):
        await svc.get_conversation(actor=same_tenant_other, conversation_id=conv.id)

    # foreign tenant
    foreign = _actor()
    with pytest.raises(ResourceNotFoundError):
        await svc.get_conversation(actor=foreign, conversation_id=conv.id)


# ---------------------------------------------------------------------------
# Tests: post_message atomicity and idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_message_commits_message_turn_run_event_job_exactly_once() -> None:
    repo = FakeRepository()
    svc = _service(repo)
    actor = _actor()

    conv_result = await svc.create_conversation(
        actor=actor, locale="en", title=None, request_id="r1", idempotency_key="ik-conv"
    )
    cid = conv_result.conversation.id

    result = await svc.post_message(
        actor=actor,
        conversation_id=cid,
        message="Hello assistant",
        locale="en",
        card_action=None,
        if_match_version=None,
        request_id="r2",
        idempotency_key="ik-msg-1",
    )

    assert result.message.content_blocks[0]["kind"] == "text"
    assert result.turn.conversation_id == cid
    assert result.run.turn_id == result.turn.id
    assert result.run.orchestrator_version == "1.0.0"
    assert result.run.orchestrator_fingerprint == "orchestrator-manifest-fingerprint"
    assert result.job.orchestration_run_id == result.run.id
    assert result.event.event_type == "assistant.turn.queued.v1"
    assert result.event.turn_id == result.turn.id
    assert result.event.orchestration_run_id == result.run.id
    assert not result.replayed
    # Exactly one of each
    assert len(repo.messages) == 1
    assert len(repo.turns) == 1
    assert len(repo.runs) == 1
    assert len(repo.jobs) == 1
    assert len(repo.events) == 1


@pytest.mark.asyncio
async def test_post_message_same_key_same_payload_replays_identical_ids() -> None:
    repo = FakeRepository()
    svc = _service(repo)
    actor = _actor()
    conv = await svc.create_conversation(
        actor=actor, locale="en", title=None, request_id="r1", idempotency_key="ik-cc"
    )
    cid = conv.conversation.id

    r1 = await svc.post_message(
        actor=actor,
        conversation_id=cid,
        message="Hi",
        locale="en",
        card_action=None,
        if_match_version=None,
        request_id="r2",
        idempotency_key="ik-m1",
    )
    r2 = await svc.post_message(
        actor=actor,
        conversation_id=cid,
        message="Hi",
        locale="en",
        card_action=None,
        if_match_version=None,
        request_id="r3",
        idempotency_key="ik-m1",
    )

    assert r2.replayed
    assert r1.message.id == r2.message.id
    assert r1.turn.id == r2.turn.id
    assert r1.run.id == r2.run.id
    # No duplicate records
    assert len(repo.messages) == 1
    assert len(repo.turns) == 1


@pytest.mark.asyncio
async def test_post_message_same_key_different_payload_conflicts() -> None:
    repo = FakeRepository()
    svc = _service(repo)
    actor = _actor()
    conv = await svc.create_conversation(
        actor=actor, locale="en", title=None, request_id="r1", idempotency_key="ik-cc2"
    )
    cid = conv.conversation.id

    await svc.post_message(
        actor=actor,
        conversation_id=cid,
        message="Hi",
        locale="en",
        card_action=None,
        if_match_version=None,
        request_id="r2",
        idempotency_key="ik-m2",
    )
    with pytest.raises(IdempotencyConflictError):
        await svc.post_message(
            actor=actor,
            conversation_id=cid,
            message="Different message",
            locale="en",
            card_action=None,
            if_match_version=None,
            request_id="r3",
            idempotency_key="ik-m2",
        )


@pytest.mark.asyncio
async def test_second_message_uses_next_persisted_message_and_event_sequences() -> None:
    repo = FakeRepository()
    svc = _service(repo)
    actor = _actor()
    conversation = await svc.create_conversation(
        actor=actor,
        locale="en",
        title=None,
        request_id="request-create",
        idempotency_key="conversation-sequence-key",
    )

    first = await svc.post_message(
        actor=actor,
        conversation_id=conversation.conversation.id,
        message="First",
        locale="en",
        card_action=None,
        if_match_version=None,
        request_id="request-first",
        idempotency_key="message-sequence-key-1",
    )
    second = await svc.post_message(
        actor=actor,
        conversation_id=conversation.conversation.id,
        message="Second",
        locale="en",
        card_action=None,
        if_match_version=None,
        request_id="request-second",
        idempotency_key="message-sequence-key-2",
    )

    assert first.message.sequence == 1
    assert first.event.sequence == 1
    assert second.message.sequence == 2
    assert second.event.sequence == 2


@pytest.mark.asyncio
async def test_post_message_does_not_call_model_or_agent_runtime() -> None:
    """Service must queue a job and return 202; it must NOT call any model inline."""
    repo = FakeRepository()
    svc = _service(repo)
    actor = _actor()
    conv = await svc.create_conversation(
        actor=actor, locale="en", title=None, request_id="r1", idempotency_key="ik-nm"
    )

    result = await svc.post_message(
        actor=actor,
        conversation_id=conv.conversation.id,
        message="Plan my project",
        locale="en",
        card_action=None,
        if_match_version=None,
        request_id="r2",
        idempotency_key="ik-nm2",
    )

    # Job is QUEUED, turn is QUEUED — no model call happened
    assert result.job.status.value == "QUEUED"
    assert result.turn.status.value == "QUEUED"
    assert result.job.job_type == "assistant.turn.execute"


# ---------------------------------------------------------------------------
# Tests: If-Match and card action validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plain_message_rejects_if_match() -> None:
    repo = FakeRepository()
    svc = _service(repo)
    actor = _actor()
    conv = await svc.create_conversation(
        actor=actor, locale="en", title=None, request_id="r1", idempotency_key="ik-im1"
    )
    with pytest.raises(AssistantServiceError) as exc_info:
        await svc.post_message(
            actor=actor,
            conversation_id=conv.conversation.id,
            message="Plain message",
            locale="en",
            card_action=None,
            if_match_version=3,  # forbidden for plain
            request_id="r2",
            idempotency_key="ik-im2",
        )
    assert "IF_MATCH_FORBIDDEN" in str(exc_info.value)


@pytest.mark.asyncio
async def test_planning_revise_requires_proposal_id_and_if_match() -> None:
    repo = FakeRepository()
    svc = _service(repo)
    actor = _actor(MembershipRole.MANAGER)
    conv = await svc.create_conversation(
        actor=actor, locale="en", title=None, request_id="r1", idempotency_key="ik-pr0"
    )
    cid = conv.conversation.id

    # PLANNING_REVISE without proposal_id raises
    with pytest.raises(AssistantServiceError) as exc_info:
        await svc.post_message(
            actor=actor,
            conversation_id=cid,
            message="Revise",
            locale="en",
            card_action={
                "kind": "PLANNING_REVISE",
                "workflow_run_id": str(uuid4()),
                "proposal_id": None,
            },
            if_match_version=1,
            request_id="r2",
            idempotency_key="ik-pr1",
        )
    assert "PROPOSAL_ID_REQUIRED" in str(exc_info.value)

    # PLANNING_REVISE without if_match raises
    with pytest.raises(AssistantServiceError) as exc_info:
        await svc.post_message(
            actor=actor,
            conversation_id=cid,
            message="Revise",
            locale="en",
            card_action={
                "kind": "PLANNING_REVISE",
                "workflow_run_id": str(uuid4()),
                "proposal_id": str(uuid4()),
            },
            if_match_version=None,  # missing
            request_id="r3",
            idempotency_key="ik-pr2",
        )
    assert "IF_MATCH_REQUIRED" in str(exc_info.value)


@pytest.mark.asyncio
async def test_planning_revise_rejects_stale_proposal_version_before_queueing() -> None:
    repo = FakeRepository()
    actor = _actor(MembershipRole.MANAGER)
    service = AssistantService(
        transaction_factory=FakeTransactionFactory(repo),
        planning_snapshot=FixedPlanningSnapshot(4),
        orchestrator_version="1.0.0",
        orchestrator_fingerprint="orchestrator-manifest-fingerprint",
    )
    conversation = await service.create_conversation(
        actor=actor,
        locale="en",
        title=None,
        request_id="request-create",
        idempotency_key="conversation-stale-key",
    )

    with pytest.raises(AssistantServiceError) as error:
        await service.post_message(
            actor=actor,
            conversation_id=conversation.conversation.id,
            message="Revise the plan",
            locale="en",
            card_action={
                "kind": "PLANNING_REVISE",
                "workflow_run_id": str(uuid4()),
                "proposal_id": str(uuid4()),
            },
            if_match_version=3,
            request_id="request-stale",
            idempotency_key="message-stale-key",
        )

    assert error.value.code == "RESOURCE_VERSION_MISMATCH"
    assert repo.messages == []
    assert repo.jobs == []


@pytest.mark.asyncio
async def test_planning_revise_persists_private_action_envelope() -> None:
    repo = FakeRepository()
    actor = _actor(MembershipRole.MANAGER)
    service = AssistantService(
        transaction_factory=FakeTransactionFactory(repo),
        planning_snapshot=FixedPlanningSnapshot(1),
        orchestrator_version="1.0.0",
        orchestrator_fingerprint="orchestrator-manifest-fingerprint",
    )
    conversation = await service.create_conversation(
        actor=actor,
        locale="vi",
        title=None,
        request_id="request-create-revision",
        idempotency_key="conversation-revision-key",
    )
    workflow_run_id = uuid4()
    proposal_id = uuid4()

    result = await service.post_message(
        actor=actor,
        conversation_id=conversation.conversation.id,
        message="Mở rộng đến cuối tháng 11",
        locale="vi",
        card_action={
            "kind": "PLANNING_REVISE",
            "workflow_run_id": str(workflow_run_id),
            "proposal_id": str(proposal_id),
        },
        if_match_version=1,
        request_id="request-submit-revision",
        idempotency_key="message-revision-key",
    )

    assert result.message.content_blocks == (
        {"kind": "text", "text": "Mở rộng đến cuối tháng 11"},
        {
            "kind": "accepted_card_action",
            "action": {
                "kind": "PLANNING_REVISE",
                "workflow_run_id": str(workflow_run_id),
                "proposal_id": str(proposal_id),
                "expected_version": 1,
            },
        },
    )


# ---------------------------------------------------------------------------
# Tests: transaction rollback atomicity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transaction_failure_rolls_back_message_turn_run_event_job_and_idempotency() -> None:
    repo = FakeRepository()
    repo.fail_after_message = True
    svc = _service(repo)
    actor = _actor()
    conv = await svc.create_conversation(
        actor=actor, locale="en", title=None, request_id="r1", idempotency_key="ik-rb-conv"
    )
    cid = conv.conversation.id
    # Reset fail flag for conversation creation, only fail during message
    repo.fail_after_message = True  # message insert triggers failure

    initial_count = len(repo.audit_log)
    _ = initial_count  # referenced for clarity only

    with pytest.raises(AssistantServiceError):
        await svc.post_message(
            actor=actor,
            conversation_id=cid,
            message="Should fail",
            locale="en",
            card_action=None,
            if_match_version=None,
            request_id="r2",
            idempotency_key="ik-rb-msg",
        )

    # After rollback: partial state should NOT persist
    assert len(repo.messages) == 0, "Message must not persist after rollback"
    assert len(repo.turns) == 0, "Turn must not persist after rollback"
    assert len(repo.runs) == 0, "Run must not persist after rollback"
    assert len(repo.jobs) == 0, "Job must not persist after rollback"
    assert len(repo.events) == 0, "Event must not persist after rollback"
    # No idempotency record for the failed message
    msg_key = (
        str(actor.membership_id),
        f"assistant.message.submit:{cid}",
        "ik-rb-msg",
    )
    assert msg_key not in repo.idempotency


@pytest.mark.asyncio
async def test_rejected_sensitive_attempt_writes_safe_audit_in_separate_transaction() -> None:
    repo = FakeRepository()
    transactions = FakeTransactionFactory(repo)
    svc = AssistantService(
        transaction_factory=transactions,
        planning_snapshot=NullPlanningSnapshot(),  # type: ignore[arg-type]
        orchestrator_version="1.0.0",
        orchestrator_fingerprint="orchestrator-manifest-fingerprint",
    )
    owner = _actor()
    conversation = await svc.create_conversation(
        actor=owner,
        locale="en",
        title=None,
        request_id="request-create-owner",
        idempotency_key="conversation-owner-key",
    )
    attacker = AuthenticatedActor(
        user_id=uuid4(),
        email="other@example.test",
        display_name="Other member",
        membership_id=uuid4(),
        organization_id=owner.organization_id,
        organization_name=owner.organization_name,
        role=MembershipRole.EMPLOYEE,
    )

    with pytest.raises(ResourceNotFoundError):
        await svc.post_message(
            actor=attacker,
            conversation_id=conversation.conversation.id,
            message="Try foreign conversation",
            locale="en",
            card_action=None,
            if_match_version=None,
            request_id="request-rejected",
            idempotency_key="rejected-message-key",
        )

    assert len(transactions.transactions) == 3
    assert repo.audit_log[-1] == {
        "action": "assistant.message.submit",
        "outcome": "REJECTED",
        "resource_type": "assistant_conversation",
        "resource_id": conversation.conversation.id,
        "request_id": "request-rejected",
        "reason_code": "RESOURCE_NOT_FOUND",
    }
    assert len(repo.conversations) == 1
    assert len(repo.messages) == 0


@pytest.mark.asyncio
async def test_retrieval_enabled_turn_has_finite_parent_allocation() -> None:
    repo = FakeRepository()

    def transactions(_: AuthenticatedActor) -> FakeTransaction:
        return FakeTransaction(repo)

    svc = AssistantService(
        transaction_factory=transactions,
        planning_snapshot=FixedPlanningSnapshot(None),
        orchestrator_version="1.0.0",
        orchestrator_fingerprint="test",
        retrieval_enabled=True,
    )
    actor = _actor()
    conversation = await svc.create_conversation(
        actor=actor,
        locale="en",
        title=None,
        request_id="retrieve-conv",
        idempotency_key="retrieve-conv",
    )
    result = await svc.post_message(
        actor=actor,
        conversation_id=conversation.conversation.id,
        message="Search public web for public planning",
        locale="en",
        card_action=None,
        if_match_version=None,
        request_id="retrieve-message",
        idempotency_key="retrieve-message",
    )
    assert result.run.budget["max_model_attempts"] == 2
    assert result.run.budget["max_tool_calls"] == 8
    assert result.run.usage == dict(
        model_attempts=0, input_tokens=0, output_tokens=0, iterations=0, tool_calls=0
    )
