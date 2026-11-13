"""Versioned, tenant-scoped scheduling hints; SQL events/jobs remain authoritative."""

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Literal, Protocol, cast
from uuid import UUID

WakeupKind = Literal["assistant.events", "workflow.events", "job.ready"]
_KINDS = frozenset({"assistant.events", "workflow.events", "job.ready"})


@dataclass(frozen=True, slots=True)
class WakeupScope:
    organization_id: UUID
    kind: WakeupKind
    resource_id: UUID

    def __post_init__(self) -> None:
        if (
            not isinstance(cast(object, self.organization_id), UUID)
            or not isinstance(cast(object, self.resource_id), UUID)
            or type(self.kind) is not str
            or self.kind not in _KINDS
        ):
            raise ValueError("INVALID_WAKEUP_SCOPE")
        if self.kind == "job.ready" and self.resource_id != self.organization_id:
            raise ValueError("INVALID_JOB_WAKEUP_SCOPE")


@dataclass(frozen=True, slots=True)
class WakeupHint:
    version: int
    organization_id: UUID
    kind: WakeupKind
    resource_id: UUID
    sequence: int | None

    def __post_init__(self) -> None:
        scope = self.scope
        if type(self.version) is not int or self.version != 1:
            raise ValueError("INVALID_WAKEUP_VERSION")
        if scope.kind == "job.ready":
            valid = self.sequence is None
        else:
            valid = type(self.sequence) is int and 0 < self.sequence <= 2**63 - 1
        if not valid:
            raise ValueError("INVALID_WAKEUP_SEQUENCE")

    @property
    def scope(self) -> WakeupScope:
        return WakeupScope(self.organization_id, self.kind, self.resource_id)

    def to_bytes(self) -> bytes:
        return json.dumps(
            dict(
                version=self.version,
                organization_id=str(self.organization_id),
                kind=self.kind,
                resource_id=str(self.resource_id),
                sequence=self.sequence,
            ),
            separators=(",", ":"),
            sort_keys=True,
        ).encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> "WakeupHint":
        if type(data) is not bytes or len(data) > 256:
            raise ValueError("INVALID_WAKEUP_PAYLOAD")
        try:
            raw: object = json.loads(data)
            if not isinstance(raw, dict):
                raise ValueError("INVALID_WAKEUP_PAYLOAD")
            values = cast(dict[str, object], raw)
            if set(values) != {"version", "organization_id", "kind", "resource_id", "sequence"}:
                raise ValueError("INVALID_WAKEUP_PAYLOAD")
            if (
                type(values["organization_id"]) is not str
                or type(values["resource_id"]) is not str
                or type(values["kind"]) is not str
            ):
                raise ValueError("INVALID_WAKEUP_PAYLOAD")
            return cls(
                cast(int, values["version"]),
                UUID(str(values["organization_id"])),
                cast(WakeupKind, values["kind"]),
                UUID(str(values["resource_id"])),
                cast(int | None, values["sequence"]),
            )
        except (ValueError, TypeError, UnicodeDecodeError) as error:
            raise ValueError("INVALID_WAKEUP_PAYLOAD") from error


class WakeupSubscription(Protocol):
    async def wait(self, *, timeout_seconds: float) -> None: ...


class WakeupPort(Protocol):
    async def publish(self, hint: WakeupHint) -> None: ...
    def subscribe(self, scope: WakeupScope) -> AbstractAsyncContextManager[WakeupSubscription]: ...


@asynccontextmanager
async def subscribe_or_poll(
    port: WakeupPort | None, scope: WakeupScope
) -> AsyncGenerator[WakeupSubscription]:
    async with AsyncExitStack() as stack:
        subscription: WakeupSubscription = PollingSubscription()
        if port is not None:
            with suppress(Exception):
                subscription = await stack.enter_async_context(port.subscribe(scope))
        yield subscription


class PollingSubscription:
    async def wait(self, *, timeout_seconds: float) -> None:
        await asyncio.sleep(max(0, timeout_seconds))


class SignalSubscription:
    def __init__(self, signal: asyncio.Event):
        self._signal = signal

    async def wait(self, *, timeout_seconds: float) -> None:
        try:
            async with asyncio.timeout(max(0, timeout_seconds)):
                await self._signal.wait()
                self._signal.clear()
        except TimeoutError:
            pass


class PendingWakeups:
    def __init__(self, *, max_scopes: int = 1000):
        self._max_scopes = max_scopes
        self._pending: dict[WakeupScope, WakeupHint] = {}

    def add(self, hint: WakeupHint) -> None:
        before = self._pending.get(hint.scope)
        if (
            before is not None
            and before.sequence is not None
            and hint.sequence is not None
            and hint.sequence <= before.sequence
        ):
            return
        if before is None and len(self._pending) >= self._max_scopes:
            return
        self._pending[hint.scope] = hint

    def drain(self) -> tuple[WakeupHint, ...]:
        hints = tuple(self._pending.values())
        self._pending.clear()
        return hints

    def pop(self) -> WakeupHint | None:
        if not self._pending:
            return None
        scope = next(iter(self._pending))
        return self._pending.pop(scope)
