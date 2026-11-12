"""Advisory same-key coordination; never a business lock or authority source."""

import asyncio
import secrets
from dataclasses import dataclass
from typing import Protocol


class CacheLeasePort(Protocol):
    async def try_acquire(self, key: str, owner: str, *, lease_ms: int) -> bool | None:
        """True owns lease, False is held, None is unavailable."""
        ...

    async def release_if_owner(self, key: str, owner: str) -> bool: ...


@dataclass(frozen=True)
class LeaseClaim:
    owner: str | None = None
    wait: bool = False


class SingleFlight:
    def __init__(self, leases: CacheLeasePort) -> None:
        self._leases = leases

    async def claim(self, key: str) -> LeaseClaim:
        owner = secrets.token_hex(16)
        acquired = await self._leases.try_acquire(key + ":lease", owner, lease_ms=5_000)
        return LeaseClaim(owner=owner if acquired else None, wait=acquired is False)

    async def release(self, key: str, owner: str) -> None:
        await self._leases.release_if_owner(key + ":lease", owner)

    async def wait(self) -> None:
        # One bounded retry; no source transaction is kept open during this wait.
        await asyncio.sleep(0.025 + secrets.randbelow(6) / 1000)
