"""Hints are bounded scheduling signals, never event data or authorization."""

from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from app.modules.assistant.application.event_service import AssistantEventService
from app.modules.assistant.domain.models import AssistantConversation, AssistantEvent
from app.modules.optimization.application.wakeup import PendingWakeups, WakeupHint, WakeupScope
from tests.test_ai_admission import actor
from tests.test_assistant_sse import EventRepository, EventTransactionFactory


def test_hint_allows_only_versioned_tenant_resource_identity():
    org, resource = uuid4(), uuid4()
    hint = WakeupHint(1, org, "assistant.events", resource, 1)
    assert hint.scope == WakeupScope(org, "assistant.events", resource)
    assert WakeupHint.from_bytes(hint.to_bytes()) == hint
    assert len(hint.to_bytes()) <= 256
    assert b"payload" not in hint.to_bytes()


@pytest.mark.parametrize(
    "change",
    [
        {"version": True},
        {"version": 2},
        {"kind": "unknown"},
        {"sequence": 0},
        {"sequence": True},
        {"sequence": None},
        {"kind": "job.ready", "sequence": 1},
    ],
)
def test_invalid_hint_contract_is_rejected(change: dict[str, object]):
    values: dict[str, Any] = dict(
        version=1, organization_id=uuid4(), kind="assistant.events", resource_id=uuid4(), sequence=1
    )
    values.update(change)
    with pytest.raises(ValueError):
        WakeupHint(**values)


def test_hint_json_rejects_extra_text_and_oversize_payloads():
    hint = WakeupHint(1, uuid4(), "assistant.events", uuid4(), 1)
    for data in (
        b"not-json",
        b"x" * 1024,
        hint.to_bytes()[:-1] + b',"payload":"private"}',
        hint.to_bytes().replace(b'"version":1', b'"version":true'),
    ):
        with pytest.raises(ValueError):
            WakeupHint.from_bytes(data)


def test_pending_hints_coalesce_by_scope_and_bound_memory():
    pending = PendingWakeups(max_scopes=2)
    org, resource = uuid4(), uuid4()
    for sequence in (3, 2, 1, 4, 4):
        pending.add(WakeupHint(1, org, "assistant.events", resource, sequence))
    pending.add(WakeupHint(1, org, "workflow.events", uuid4(), 1))
    pending.add(WakeupHint(1, org, "workflow.events", uuid4(), 1))
    hints = pending.drain()
    assert len(hints) == 2
    assert hints[0].sequence == 4
    assert pending.drain() == ()


class Waker:
    def __init__(
        self, enter: Callable[[], None] | None = None, wait: Callable[[], None] | None = None
    ):
        self.enter = enter
        self.on_wait = wait
        self.waits: list[float] = []
        self.subscribed = False

    async def publish(self, hint: WakeupHint) -> None:
        pass

    @asynccontextmanager
    async def subscribe(self, scope: WakeupScope):
        self.subscribed = True
        if self.enter:
            self.enter()
        try:
            yield self
        finally:
            self.subscribed = False

    async def wait(self, *, timeout_seconds: float) -> None:
        self.waits.append(timeout_seconds)
        if self.on_wait:
            self.on_wait()


def source_fixture():
    owner = actor()
    conversation = AssistantConversation.create(
        organization_id=owner.organization_id, owner_membership_id=owner.membership_id, locale="en"
    )
    repository = EventRepository(conversation=conversation, events=[])
    event = AssistantEvent(
        id=uuid4(),
        organization_id=owner.organization_id,
        conversation_id=conversation.id,
        sequence=1,
        event_type="assistant.message.created.v1",
        public_payload={"text": "committed"},
        occurred_at=datetime.now(UTC),
    )
    return owner, conversation, repository, event


@pytest.mark.asyncio
async def test_commit_between_subscribe_and_replay_is_not_lost():
    owner, conversation, repository, event = source_fixture()
    wake = Waker(enter=lambda: repository.events.append(event))
    service = AssistantEventService(
        transaction_factory=EventTransactionFactory(repository), wakeup=wake
    )
    stream = service.stream(actor=owner, conversation_id=conversation.id, after_sequence=0)
    try:
        frame = await anext(stream)
        assert wake.subscribed and frame.startswith("id: 1\n")
        assert repository.job_count == 0
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_lost_hint_catches_up_with_a_poll_at_most_fifteen_seconds():
    owner, conversation, repository, event = source_fixture()
    wake = Waker(wait=lambda: repository.events.append(event))
    service = AssistantEventService(
        transaction_factory=EventTransactionFactory(repository), wakeup=wake, heartbeat_seconds=99
    )
    stream = service.stream(actor=owner, conversation_id=conversation.id, after_sequence=0)
    try:
        assert (await anext(stream)).startswith(": heartbeat")
        assert (await anext(stream)).startswith("id: 1\n")
        assert max(wake.waits) <= 15
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_same_tenant_other_owner_hint_does_not_authorize_read():
    owner, conversation, repository, event = source_fixture()
    repository.events.append(event)
    other = replace(owner, membership_id=uuid4(), user_id=uuid4())
    service = AssistantEventService(
        transaction_factory=EventTransactionFactory(repository), wakeup=Waker()
    )
    frames = [
        frame
        async for frame in service.stream(
            actor=other, conversation_id=conversation.id, after_sequence=0
        )
    ]
    assert frames == [] and repository.job_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["archive", "delete", "revoke"])
async def test_stream_rechecks_access_after_open(change: str):
    from app.modules.assistant.domain.models import ConversationStatus

    owner, conversation, repository, event = source_fixture()

    class Actors:
        revoked = False

        async def resolve(self, **_: Any):
            return None if self.revoked else owner

    actors = Actors()

    def mutate():
        repository.events.append(event)
        if change == "archive":
            repository.conversation = replace(conversation, status=ConversationStatus.ARCHIVED)
        elif change == "delete":
            repository.conversation = None
        else:
            actors.revoked = True

    service = AssistantEventService(
        transaction_factory=EventTransactionFactory(repository),
        wakeup=Waker(wait=mutate),
        actors=actors,
    )
    stream = service.stream(actor=owner, conversation_id=conversation.id, after_sequence=0)
    assert (await anext(stream)).startswith(": heartbeat")
    frames = [frame async for frame in stream]
    assert len(frames) == (1 if change == "archive" else 0)
    assert repository.job_count == 0


@pytest.mark.asyncio
async def test_signal_wait_cancellation_propagates():
    import asyncio

    from app.modules.optimization.application.wakeup import SignalSubscription

    signal = asyncio.Event()
    task = asyncio.create_task(SignalSubscription(signal).wait(timeout_seconds=15))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
