"""Replayable, sanitized workflow event streaming with short transactions."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import cast
from uuid import UUID

from anyio import CancelScope

from app.modules.identity.application.ports import CurrentActorReadPort
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.optimization.application.wakeup import WakeupPort, WakeupScope, subscribe_or_poll
from app.modules.organization.domain.roles import MembershipRole
from app.modules.planning_runs.application.ports import PlanningRunTransaction
from app.modules.planning_runs.domain.models import (
    PlanningRunForbiddenError,
    PlanningRunNotFoundError,
    WorkflowEvent,
    WorkflowRun,
)

_READ_ROLES = frozenset({MembershipRole.ADMIN, MembershipRole.MANAGER})
_SENSITIVE_KEYS = (
    "prompt",
    "secret",
    "token",
    "reasoning",
    "chain_of_thought",
    "stack",
    "provider_error",
)


def _sanitize(value: object) -> object:
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        return {
            str(key): _sanitize(item)
            for key, item in mapping.items()
            if not any(part in str(key).casefold() for part in _SENSITIVE_KEYS)
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in cast(list[object] | tuple[object, ...], value)]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _frame(event: WorkflowEvent) -> str:
    payload = json.dumps(
        _sanitize(event.public_payload),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"id: {event.sequence}\nevent: {event.event_type}\ndata: {payload}\n\n"


class WorkflowEventService:
    def __init__(
        self,
        *,
        transaction_factory: Callable[[AuthenticatedActor], PlanningRunTransaction],
        heartbeat_seconds: float = 15,
        wakeup: WakeupPort | None = None,
        actors: CurrentActorReadPort | None = None,
    ) -> None:
        self._transaction_factory = transaction_factory
        self._heartbeat_seconds = heartbeat_seconds
        self._wakeup = wakeup
        self._actors = actors

    async def _current_actor(self, actor: AuthenticatedActor) -> AuthenticatedActor:
        current = (
            actor
            if self._actors is None
            else await self._actors.resolve(
                organization_id=actor.organization_id, membership_id=actor.membership_id
            )
        )
        if current is None or (current.organization_id, current.membership_id, current.user_id) != (
            actor.organization_id,
            actor.membership_id,
            actor.user_id,
        ):
            raise PlanningRunNotFoundError
        if current.role not in _READ_ROLES:
            raise PlanningRunForbiddenError
        return current

    async def authorize(self, *, actor: AuthenticatedActor, run_id: UUID) -> WorkflowRun:
        actor = await self._current_actor(actor)
        async with self._transaction_factory(actor) as transaction:
            run = await transaction.repository.get_workflow_run(actor=actor, run_id=run_id)
        if run is None:
            raise PlanningRunNotFoundError
        return run

    async def _read(
        self, *, actor: AuthenticatedActor, run_id: UUID, after_sequence: int
    ) -> tuple[WorkflowRun, list[WorkflowEvent]]:
        actor = await self._current_actor(actor)
        async with self._transaction_factory(actor) as transaction:
            run = await transaction.repository.get_workflow_run(actor=actor, run_id=run_id)
            if run is None:
                raise PlanningRunNotFoundError
            events = await transaction.repository.list_events(
                actor=actor,
                run_id=run_id,
                after_sequence=after_sequence,
            )
        return run, events

    async def replay(
        self, *, actor: AuthenticatedActor, run_id: UUID, after_sequence: int
    ) -> list[str]:
        if actor.role not in _READ_ROLES:
            raise PlanningRunForbiddenError
        _, events = await self._read(actor=actor, run_id=run_id, after_sequence=after_sequence)
        return [_frame(event) for event in events]

    async def stream(
        self, *, actor: AuthenticatedActor, run_id: UUID, after_sequence: int
    ) -> AsyncIterator[str]:
        cursor = after_sequence
        clock = asyncio.get_running_loop().time
        heartbeat_at = clock()
        scope = WakeupScope(actor.organization_id, "workflow.events", run_id)
        async with subscribe_or_poll(self._wakeup, scope) as subscription:
            while True:
                run: WorkflowRun | None = None
                events: list[WorkflowEvent] = []
                try:
                    with CancelScope(shield=True):
                        run, events = await self._read(
                            actor=actor, run_id=run_id, after_sequence=cursor
                        )
                except (PlanningRunForbiddenError, PlanningRunNotFoundError):
                    return
                if run is None:
                    return
                for event in events:
                    cursor = event.sequence
                    yield _frame(event)
                if run.status.is_terminal:
                    return
                if events:
                    heartbeat_at = clock() + self._heartbeat_seconds
                elif clock() >= heartbeat_at:
                    yield ": heartbeat\n\n"
                    heartbeat_at = clock() + self._heartbeat_seconds
                await subscription.wait(timeout_seconds=min(15, max(0, heartbeat_at - clock())))
