"""Immutable cache identities from authenticated scope and durable revisions."""

import re
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CacheScope:
    organization_id: UUID
    membership_id: UUID
    permission_hash: str


@dataclass(frozen=True, slots=True)
class CacheRevision:
    epoch: UUID
    generation: int

    def __post_init__(self) -> None:
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("INVALID_CACHE_REVISION")


@dataclass(frozen=True, slots=True)
class ReadCacheKey:
    namespace: str
    family: str
    dto_version: str
    scope: CacheScope
    revision: CacheRevision
    query_hash: str

    def encode(self) -> str:
        if (
            re.fullmatch(r"[a-z0-9:_-]{1,64}", self.namespace) is None
            or re.fullmatch(r"[a-z0-9_.-]{1,64}", self.family) is None
            or re.fullmatch(r"[a-z0-9_.-]{1,32}", self.dto_version) is None
            or re.fullmatch(r"[a-f0-9]{64}", self.scope.permission_hash) is None
            or re.fullmatch(r"[a-f0-9]{64}", self.query_hash) is None
        ):
            raise ValueError("INVALID_CACHE_KEY")
        return (
            f"wm:{self.namespace}:redis-v1:org:{self.scope.organization_id}:"
            f"actor:{self.scope.membership_id}:{self.family}:{self.dto_version}:"
            f"{self.revision.epoch}:{self.revision.generation}:"
            f"{self.scope.permission_hash}:{self.query_hash}"
        )
