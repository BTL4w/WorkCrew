"""Unavailable caches miss; generation reads belong to the source snapshot."""

from contextvars import ContextVar
from typing import Protocol
from uuid import UUID

from .cache_contracts import CacheRevision

# Operational timing context only; never tenant/permission or business state.
CACHE_OPERATION_DEADLINE: ContextVar[float | None] = ContextVar(
    "cache_operation_deadline",
    default=None,
)


class ReadCachePort(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def put(self, key: str, payload: bytes, *, ttl_ms: int) -> bool: ...


class GenerationReadPort(Protocol):
    async def current(self, organization_id: UUID) -> CacheRevision | None: ...
