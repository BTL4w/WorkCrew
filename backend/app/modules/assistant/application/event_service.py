"""Persisted SSE event service for Assistant conversations — Task 6.

Replays committed assistant_events in ascending sequence. Never enqueues
new work on reconnect. PostgreSQL is the source of truth; optional Redis hints
only reduce the wait before the next authorized SQL read.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import replace
from typing import Any, cast
from uuid import UUID

from anyio import CancelScope

from app.modules.assistant.application.service import ResourceNotFoundError
from app.modules.assistant.domain.models import AssistantEvent, ConversationStatus
from app.modules.identity.application.ports import CurrentActorReadPort
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.optimization.application.wakeup import WakeupPort, WakeupScope, subscribe_or_poll


def _sanitize(value: object) -> object:
    """Remove sensitive keys recursively — never expose prompt, secret, stack, etc."""
    _SENSITIVE = (
        "prompt",
        "secret",
        "token",
        "reasoning",
        "chain_of_thought",
        "stack",
        "provider_error",
    )
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        return {
            str(k): _sanitize(v)
            for k, v in mapping.items()
            if not any(part in str(k).casefold() for part in _SENSITIVE)
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in cast(list[object] | tuple[object, ...], value)]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _frame(event: AssistantEvent) -> str:
    payload = json.dumps(
        _sanitize(event.public_payload),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"id: {event.sequence}\nevent: {event.event_type}\ndata: {payload}\n\n"


class AssistantEventService:
    """SSE streaming service over persisted assistant_events.

    Contract:
    - authorize() must be called before streaming; raises ResourceNotFoundError
      if conversation not owned by actor.
    - stream() replays committed events in order, emits keepalive comments
      when no new events, and terminates when the owning Turn is terminal.
    - Reconnect with Last-Event-ID resumes from the next sequence after the ID.
    - No new Turn, job or model call is ever started.
    """

    def __init__(
        self,
        *,
        transaction_factory: Any,
        heartbeat_seconds: float = 15.0,
        block_projector: Any = None,
        wakeup: WakeupPort | None = None,
        actors: CurrentActorReadPort | None = None,
    ) -> None:
        self.block_projector = block_projector
        self._transactions = transaction_factory
        self._heartbeat_seconds = heartbeat_seconds
        self._wakeup = wakeup
        self._actors = actors

    async def _current_actor(self, actor: AuthenticatedActor) -> AuthenticatedActor | None:
        if self._actors is None:
            return actor
        current = await self._actors.resolve(
            organization_id=actor.organization_id, membership_id=actor.membership_id
        )
        if current is None or (current.organization_id, current.membership_id, current.user_id) != (
            actor.organization_id,
            actor.membership_id,
            actor.user_id,
        ):
            return None
        return current

    async def authorize(self, *, actor: AuthenticatedActor, conversation_id: UUID) -> None:
        """Verify actor owns the conversation; raise ResourceNotFoundError otherwise."""
        current = await self._current_actor(actor)
        if current is None:
            raise ResourceNotFoundError()
        actor = current
        async with self._transactions(actor) as txn:
            snapshot = await txn.repository.get_conversation_snapshot(
                actor=actor, conversation_id=conversation_id
            )
            await txn.commit()
        if snapshot is None:
            raise ResourceNotFoundError()

    async def _read_events(
        self,
        *,
        actor: AuthenticatedActor,
        conversation_id: UUID,
        after_sequence: int,
    ) -> tuple[bool, list[AssistantEvent]]:
        """Returns (conversation_is_terminal, events_after_sequence)."""
        current = await self._current_actor(actor)
        if current is None:
            return True, []
        actor = current
        async with self._transactions(actor) as txn:
            snapshot = await txn.repository.get_conversation_snapshot(
                actor=actor, conversation_id=conversation_id
            )
            if snapshot is None:
                return True, []
            events = await txn.repository.list_events(
                actor=actor,
                conversation_id=conversation_id,
                after_sequence=after_sequence,
            )
            await txn.commit()
        if self.block_projector is not None:
            events = [
                replace(
                    e, public_payload=await self.block_projector.project(actor, e.public_payload)
                )
                for e in events
            ]
        is_archived = snapshot.conversation.status is ConversationStatus.ARCHIVED
        return is_archived, sorted(events, key=lambda event: event.sequence)

    async def replay(
        self,
        *,
        actor: AuthenticatedActor,
        conversation_id: UUID,
        after_sequence: int,
    ) -> list[str]:
        """Return all frames after `after_sequence` (for bounded test use)."""
        _, events = await self._read_events(
            actor=actor, conversation_id=conversation_id, after_sequence=after_sequence
        )
        return [_frame(e) for e in events]

    async def stream(
        self,
        *,
        actor: AuthenticatedActor,
        conversation_id: UUID,
        after_sequence: int,
    ) -> AsyncGenerator[str]:
        """Async generator that yields SSE frames and keepalive comments."""
        cursor = after_sequence
        clock = asyncio.get_running_loop().time
        heartbeat_at = clock()
        scope = WakeupScope(actor.organization_id, "assistant.events", conversation_id)
        async with subscribe_or_poll(self._wakeup, scope) as subscription:
            while True:
                is_terminal = False
                events: list[AssistantEvent] = []
                # Release every SQL transaction before a wait/send/cancellation boundary.
                with CancelScope(shield=True):
                    is_terminal, events = await self._read_events(
                        actor=actor, conversation_id=conversation_id, after_sequence=cursor
                    )
                for event in events:
                    cursor = event.sequence
                    yield _frame(event)
                if is_terminal:
                    return
                if events:
                    heartbeat_at = clock() + self._heartbeat_seconds
                elif clock() >= heartbeat_at:
                    yield ": heartbeat\n\n"
                    heartbeat_at = clock() + self._heartbeat_seconds
                await subscription.wait(timeout_seconds=min(15, max(0, heartbeat_at - clock())))
