"""Persisted SSE event service for Assistant conversations — Task 6.

Replays committed assistant_events in ascending sequence. Never enqueues
new work on reconnect. Uses PostgreSQL as the source of truth (no Redis/broker).
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
from app.modules.identity.domain.auth import AuthenticatedActor


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
    ) -> None:
        self.block_projector = block_projector
        self._transactions = transaction_factory
        self._heartbeat_seconds = heartbeat_seconds

    async def authorize(self, *, actor: AuthenticatedActor, conversation_id: UUID) -> None:
        """Verify actor owns the conversation; raise ResourceNotFoundError otherwise."""
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
        while True:
            is_terminal = False
            events: list[AssistantEvent] = []
            # Starlette cancels this generator as soon as the browser closes an
            # EventSource.  Do not let that cancellation interrupt psycopg while
            # it is checking out or querying a pooled connection: psycopg cannot
            # safely return a connection whose protocol state is still ACTIVE.
            # Each read is short and bounded; cancellation is observed at the
            # sleep/send boundary immediately after the transaction is closed.
            with CancelScope(shield=True):
                is_terminal, events = await self._read_events(
                    actor=actor, conversation_id=conversation_id, after_sequence=cursor
                )
            for event in events:
                cursor = event.sequence
                yield _frame(event)
            if is_terminal:
                return
            if not events:
                yield ": heartbeat\n\n"
            await asyncio.sleep(self._heartbeat_seconds)
