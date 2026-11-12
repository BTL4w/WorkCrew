"""Bounded, non-authoritative Redis DTO operations; failures become misses."""

import asyncio
from collections.abc import Callable
from time import monotonic, perf_counter

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.modules.optimization.application.cache_ports import CACHE_OPERATION_DEADLINE


class DisabledReadCache:
    async def get(self, key: str) -> bytes | None:
        return None

    async def put(self, key: str, payload: bytes, *, ttl_ms: int) -> bool:
        return False


class RedisReadCache:
    def __init__(
        self, client: Redis, namespace: str, clock: Callable[[], float] = monotonic
    ) -> None:
        self._client = client
        self._prefix = f"wm:{namespace}:redis-v1:"
        self._clock = clock
        self._failures = 0
        self._open_until = 0.0
        self._probing = False
        self._epoch = 0

    def _permit(self) -> tuple[int, bool] | None:
        if self._open_until == 0:
            return self._epoch, False
        if self._clock() < self._open_until or self._probing:
            return None
        self._probing = True
        return self._epoch, True

    def _success(self, ticket: tuple[int, bool]) -> None:
        if ticket[0] != self._epoch:
            return
        self._failures = 0
        self._open_until = 0
        self._probing = False

    def _failure(self, ticket: tuple[int, bool]) -> None:
        if ticket[0] != self._epoch:
            return
        self._probing = False
        self._failures += 1
        if self._failures >= 3:
            self._open_until = self._clock() + 5
            self._epoch += 1

    def _cancelled(self, ticket: tuple[int, bool]) -> None:
        deadline = CACHE_OPERATION_DEADLINE.get()
        if deadline is not None and perf_counter() >= deadline:
            self._failure(ticket)
        elif ticket[0] == self._epoch and ticket[1]:
            self._probing = False

    def allow_leases(self) -> bool:
        # Cache GET is the single half-open probe; leases must not bypass it.
        return self._open_until == 0

    async def get(self, key: str) -> bytes | None:
        if not key.startswith(self._prefix):
            return None
        ticket = self._permit()
        if ticket is None:
            return None
        try:
            async with asyncio.timeout(0.05):
                result = await self._client.get(key)
                self._success(ticket)
                return result if isinstance(result, bytes) and len(result) <= 256 * 1024 else None
        except (RedisError, TimeoutError, OSError):
            self._failure(ticket)
            return None
        except asyncio.CancelledError:
            self._cancelled(ticket)
            raise

    async def put(self, key: str, payload: bytes, *, ttl_ms: int) -> bool:
        if (
            not key.startswith(self._prefix)
            or len(payload) > 256 * 1024
            or not 0 < ttl_ms <= 300_000
        ):
            return False
        ticket = self._permit()
        if ticket is None:
            return False
        try:
            async with asyncio.timeout(0.05):
                result = bool(await self._client.set(key, payload, px=ttl_ms))
                self._success(ticket)
                return result
        except (RedisError, TimeoutError, OSError):
            self._failure(ticket)
            return False
        except asyncio.CancelledError:
            self._cancelled(ticket)
            raise
