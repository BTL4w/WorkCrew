"""Bounded, non-authoritative Redis DTO operations; failures become misses."""

import asyncio

from redis.asyncio import Redis
from redis.exceptions import RedisError


class DisabledReadCache:
    async def get(self, key: str) -> bytes | None:
        return None

    async def put(self, key: str, payload: bytes, *, ttl_ms: int) -> bool:
        return False


class RedisReadCache:
    def __init__(self, client: Redis, namespace: str) -> None:
        self._client = client
        self._prefix = f"wm:{namespace}:redis-v1:"

    async def get(self, key: str) -> bytes | None:
        if not key.startswith(self._prefix):
            return None
        try:
            async with asyncio.timeout(0.05):
                result = await self._client.get(key)
                return result if isinstance(result, bytes) and len(result) <= 256 * 1024 else None
        except (RedisError, TimeoutError, OSError):
            return None

    async def put(self, key: str, payload: bytes, *, ttl_ms: int) -> bool:
        if (
            not key.startswith(self._prefix)
            or len(payload) > 256 * 1024
            or not 0 < ttl_ms <= 300_000
        ):
            return False
        try:
            async with asyncio.timeout(0.05):
                return bool(await self._client.set(key, payload, px=ttl_ms))
        except (RedisError, TimeoutError, OSError):
            return False
