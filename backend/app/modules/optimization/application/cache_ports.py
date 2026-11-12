"""Unavailable caches miss; generation reads belong to the source snapshot."""

from typing import Protocol
from uuid import UUID

from .cache_contracts import CacheRevision


class ReadCachePort(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def put(self, key: str, payload: bytes, *, ttl_ms: int) -> bool: ...


class GenerationReadPort(Protocol):
    async def current(self, organization_id: UUID) -> CacheRevision | None: ...
