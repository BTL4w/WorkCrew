"""Redis leases reduce duplicate computation; SQL owns mutation correctness."""

import asyncio
from collections.abc import Callable

from redis.asyncio import Redis
from redis.exceptions import RedisError

_RELEASE = """if redis.call('GET',KEYS[1]) == ARGV[1] then
return redis.call('DEL',KEYS[1]) else return 0 end"""


class RedisCacheLeases:
    def __init__(
        self, client: Redis, namespace: str, *, available: Callable[[], bool] | None = None
    ) -> None:
        self._client = client
        self._prefix = f"wm:{namespace}:redis-v1:"
        self._available = available or (lambda: True)

    async def try_acquire(self, key: str, owner: str, *, lease_ms: int) -> bool | None:
        if not key.startswith(self._prefix) or not 0 < lease_ms <= 5_000 or not self._available():
            return None
        try:
            async with asyncio.timeout(0.025):
                return bool(await self._client.set(key, owner, nx=True, px=lease_ms))
        except (RedisError, TimeoutError, OSError):
            return None

    async def release_if_owner(self, key: str, owner: str) -> bool:
        if not key.startswith(self._prefix):
            return False
        try:
            async with asyncio.timeout(0.025):
                return bool(await self._client.eval(_RELEASE, 1, key, owner))
        except (RedisError, TimeoutError, OSError):
            return False
