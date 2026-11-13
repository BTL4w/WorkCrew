"""One multiplexed subscriber and bounded best-effort post-commit publisher."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Protocol, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.modules.optimization.application.wakeup import (
    PendingWakeups,
    PollingSubscription,
    SignalSubscription,
    WakeupHint,
    WakeupScope,
    WakeupSubscription,
)


class _Publisher(Protocol):
    async def publish(self, channel: str, message: bytes) -> int: ...


class _PubSub(Protocol):
    async def subscribe(self, *channels: str) -> None: ...
    async def unsubscribe(self, *channels: str) -> None: ...
    async def get_message(
        self,
        *,
        ignore_subscribe_messages: bool,
        timeout: float,  # noqa: ASYNC109 -- SDK signature
    ) -> object: ...


class _Subscriber(Protocol):
    def pubsub(self) -> AbstractAsyncContextManager[_PubSub]: ...
    async def aclose(self) -> None: ...


@dataclass
class _Scope:
    signal: asyncio.Event = field(default_factory=asyncio.Event)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    refs: int = 0
    sequence: int = 0


class RedisWakeup:
    def __init__(
        self, commands: Redis, subscriber: Redis, namespace: str, *, max_scopes: int = 1000
    ):
        self._commands = cast(_Publisher, commands)
        self._subscriber = cast(_Subscriber, subscriber)
        self._namespace = namespace
        self._max_scopes = max_scopes
        self._scopes: dict[str, _Scope] = {}
        self._changed = asyncio.Event()
        self._send_signal = asyncio.Event()
        self._pending = PendingWakeups(max_scopes=max_scopes)
        self._reader: asyncio.Task[None] | None = None
        self._sender: asyncio.Task[None] | None = None
        self._closed = False
        self._unavailable = False
        self._publish_cooldown = 0.0
        self.subscriber_connections = 0
        self.publish_failures = 0
        self.malformed_hints = 0
        self.received_signals = 0
        self.subscription_acks = 0

    @property
    def active_scopes(self) -> int:
        return len(self._scopes)

    def channel(self, scope: WakeupScope) -> str:
        return (
            f"wm:{self._namespace}:wake:org:{scope.organization_id}:"
            f"{scope.kind}:{scope.resource_id}"
        )

    async def publish(self, hint: WakeupHint) -> None:
        if self._closed or asyncio.get_running_loop().time() < self._publish_cooldown:
            return
        self._pending.add(hint)
        self._send_signal.set()
        if self._sender is None:
            self._sender = asyncio.create_task(self._send(), name="redis-wakeup-publisher")

    async def _send(self) -> None:
        while not self._closed:
            hint = self._pending.pop()
            if hint is None:
                self._send_signal.clear()
                await self._send_signal.wait()
                continue
            try:
                async with asyncio.timeout(0.025):
                    await self._commands.publish(self.channel(hint.scope), hint.to_bytes())
            except (RedisError, OSError, TimeoutError):
                self.publish_failures += 1
                self._publish_cooldown = asyncio.get_running_loop().time() + 0.25
                self._pending.drain()

    @asynccontextmanager
    async def subscribe(self, scope: WakeupScope) -> AsyncGenerator[WakeupSubscription]:
        channel = self.channel(scope)
        if self._closed or (channel not in self._scopes and len(self._scopes) >= self._max_scopes):
            yield PollingSubscription()
            return
        entry = self._scopes.setdefault(channel, _Scope())
        entry.refs += 1
        self._changed.set()
        if self._reader is None:
            self._reader = asyncio.create_task(self._listen(), name="redis-wakeup-subscriber")
        try:
            if not self._unavailable:
                try:
                    async with asyncio.timeout(0.2):
                        await entry.ready.wait()
                except TimeoutError:
                    pass
            yield SignalSubscription(entry.signal)
        finally:
            entry.refs -= 1
            if entry.refs == 0:
                self._scopes.pop(channel, None)
            self._changed.set()

    def _message(self, message: object) -> None:
        if not isinstance(message, dict):
            return
        values = cast(dict[str, object], message)
        raw_channel = values.get("channel")
        if type(raw_channel) is not bytes:
            return
        try:
            channel = raw_channel.decode("ascii")
        except UnicodeDecodeError:
            return
        entry = self._scopes.get(channel)
        if entry is None:
            return
        if values.get("type") == "subscribe":
            self.subscription_acks += 1
            self._unavailable = False
            entry.ready.set()
            entry.signal.set()  # Reconnect/subscribe always catches up from SQL.
            return
        if values.get("type") != "message":
            return
        try:
            data = values.get("data")
            if type(data) is not bytes:
                raise ValueError("INVALID_WAKEUP_PAYLOAD")
            hint = WakeupHint.from_bytes(data)
            if self.channel(hint.scope) != channel:
                raise ValueError("WAKEUP_SCOPE_MISMATCH")
        except ValueError:
            self.malformed_hints += 1
            return
        if hint.sequence is not None:
            if hint.sequence <= entry.sequence:
                return
            entry.sequence = hint.sequence
        self.received_signals += 1
        entry.signal.set()

    async def _listen(self) -> None:
        while not self._closed:
            if not self._scopes:
                self._changed.clear()
                await self._changed.wait()
                continue
            try:
                async with self._subscriber.pubsub() as pubsub:
                    active: set[str] = set()
                    self.subscriber_connections = 1
                    while self._scopes and not self._closed:
                        desired = set(self._scopes)
                        added, removed = desired - active, active - desired
                        async with asyncio.timeout(0.1):
                            if added:
                                await pubsub.subscribe(*sorted(added))
                            if removed:
                                await pubsub.unsubscribe(*sorted(removed))
                        active = desired
                        message: object = await pubsub.get_message(
                            ignore_subscribe_messages=False, timeout=0.05
                        )
                        self._message(message)
            except (RedisError, OSError, TimeoutError):
                self._unavailable = True
                for entry in self._scopes.values():
                    if entry.ready.is_set():
                        entry.signal.set()
                    entry.ready.clear()
                await asyncio.sleep(0.25)
            finally:
                self.subscriber_connections = 0

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = [task for task in (self._sender, self._reader) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for entry in self._scopes.values():
            entry.signal.set()
        self._scopes.clear()
        self._pending.drain()
        await self._subscriber.aclose()
