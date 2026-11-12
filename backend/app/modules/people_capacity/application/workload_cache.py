"""Actor-specific workload DTO cache; source transactions own authorization."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from time import perf_counter
from typing import Literal, Protocol, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.optimization.application.cache_contracts import (
    CacheRevision,
    CacheScope,
    ReadCacheKey,
)
from app.modules.optimization.application.cache_ports import ReadCachePort
from app.modules.organization.domain.roles import MembershipRole
from app.modules.people_capacity.domain.skills import PeopleSkillForbiddenError
from app.modules.people_capacity.domain.workload import WeeklyWorkload

_MAX_BYTES = 256 * 1024
_TTL_SECONDS = 60


class WorkloadReadSnapshot(Protocol):
    actor: AuthenticatedActor
    revision: CacheRevision | None

    async def load(
        self, *, week_start: date, membership_id: UUID | None
    ) -> tuple[WeeklyWorkload, ...]: ...


type WorkloadReadTransactions = Callable[
    [AuthenticatedActor], AbstractAsyncContextManager[WorkloadReadSnapshot]
]


class _WorkloadRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)
    membership_id: UUID
    project_week_id: UUID
    effective_capacity_hours: int = Field(strict=True, ge=0, le=168)
    allocated_effort_hours: int = Field(strict=True, ge=0)
    residual_capacity_hours: int = Field(strict=True, ge=0)
    workload_ratio: Decimal | None = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def verify_arithmetic(self) -> Self:
        expected = (
            None
            if self.effective_capacity_hours == 0
            else Decimal(self.allocated_effort_hours) / Decimal(self.effective_capacity_hours)
        )
        if (
            self.residual_capacity_hours
            != max(self.effective_capacity_hours - self.allocated_effort_hours, 0)
            or self.workload_ratio != expected
        ):
            raise ValueError("INVALID_CACHED_WORKLOAD")
        return self


class _Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"] = "1"
    key: str
    organization_id: UUID
    membership_id: UUID
    revision: CacheRevision
    created_at: AwareDatetime
    expires_at: AwareDatetime
    rows: tuple[_WorkloadRow, ...] = Field(max_length=10_000)


def _decode(raw: bytes, key: ReadCacheKey, now: datetime) -> tuple[WeeklyWorkload, ...] | None:
    if len(raw) > _MAX_BYTES:
        return None
    try:
        envelope = _Envelope.model_validate_json(raw)
        if (
            envelope.key != key.encode()
            or envelope.organization_id != key.scope.organization_id
            or envelope.membership_id != key.scope.membership_id
            or envelope.revision != key.revision
            or not envelope.created_at <= now < envelope.expires_at
            or envelope.expires_at > envelope.created_at + timedelta(seconds=_TTL_SECONDS)
        ):
            return None
        return tuple(WeeklyWorkload(**row.model_dump()) for row in envelope.rows)
    except (ValidationError, ValueError):
        return None


class WorkloadCacheReader:
    def __init__(
        self,
        *,
        transactions: WorkloadReadTransactions,
        cache: ReadCachePort,
        namespace: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._transactions = transactions
        self._cache = cache
        self._namespace = namespace
        self._clock = clock or (lambda: datetime.now(UTC))

    async def read(
        self, *, actor: AuthenticatedActor, week_start: date, membership_id: UUID | None
    ) -> tuple[WeeklyWorkload, ...]:
        key: ReadCacheKey | None = None
        remaining_cache_seconds = 0.05
        async with self._transactions(actor) as snapshot:
            current = snapshot.actor
            if (
                current.organization_id != actor.organization_id
                or current.membership_id != actor.membership_id
                or current.user_id != actor.user_id
            ):
                raise PeopleSkillForbiddenError
            if current.role is MembershipRole.EMPLOYEE:
                if membership_id is not None and membership_id != current.membership_id:
                    raise PeopleSkillForbiddenError
                membership_id = current.membership_id
            if snapshot.revision is not None:
                permission = hashlib.sha256(
                    f"workload.read.v1:{current.role.value}".encode()
                ).hexdigest()
                query = json.dumps(
                    {"week_start": str(week_start), "membership_id": str(membership_id)},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                key = ReadCacheKey(
                    self._namespace,
                    "workload.weekly",
                    "1",
                    CacheScope(current.organization_id, current.membership_id, permission),
                    snapshot.revision,
                    hashlib.sha256(query.encode()).hexdigest(),
                )
                started = perf_counter()
                try:
                    async with asyncio.timeout(remaining_cache_seconds):
                        raw = await self._cache.get(key.encode())
                except TimeoutError:
                    raw = None
                remaining_cache_seconds -= perf_counter() - started
                if raw is not None:
                    cached = _decode(raw, key, self._clock())
                    if cached is not None and (
                        membership_id is None
                        or all(row.membership_id == membership_id for row in cached)
                    ):
                        return cached
            value = await snapshot.load(week_start=week_start, membership_id=membership_id)
        if key is not None and remaining_cache_seconds > 0 and len(value) <= 10_000:
            now = self._clock()
            ttl_ms = (_TTL_SECONDS * 1000) - secrets.randbelow(6_001)
            envelope = _Envelope(
                key=key.encode(),
                organization_id=key.scope.organization_id,
                membership_id=key.scope.membership_id,
                revision=key.revision,
                created_at=now,
                expires_at=now + timedelta(milliseconds=ttl_ms),
                rows=tuple(_WorkloadRow.model_validate(row) for row in value),
            )
            raw = envelope.model_dump_json().encode()
            if len(raw) <= _MAX_BYTES:
                try:
                    async with asyncio.timeout(remaining_cache_seconds):
                        await self._cache.put(key.encode(), raw, ttl_ms=ttl_ms)
                except TimeoutError:
                    pass
        return value
