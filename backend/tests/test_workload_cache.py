"""Cache hits must preserve live scope and a consistent source revision."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.people_capacity.domain.skills import PeopleSkillForbiddenError
from app.modules.people_capacity.domain.workload import WeeklyWorkload


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    async def put(self, key: str, payload: bytes, *, ttl_ms: int) -> bool:
        self.values[key] = payload
        return True


class Source:
    def __init__(self, actor: AuthenticatedActor) -> None:
        from app.modules.optimization.application.cache_contracts import CacheRevision

        self.actor = actor
        self.revision: Any = CacheRevision(epoch=uuid4(), generation=1)
        self.values: tuple[WeeklyWorkload, ...] = (
            WeeklyWorkload(actor.membership_id, uuid4(), 8, 4, 4, Decimal("0.5")),
        )
        self.loads = 0

    @asynccontextmanager
    async def __call__(self, actor: AuthenticatedActor) -> AsyncGenerator[Any]:
        yield self

    async def load(
        self, *, week_start: date, membership_id: UUID | None
    ) -> tuple[WeeklyWorkload, ...]:
        self.loads += 1
        return tuple(
            row
            for row in self.values
            if membership_id is None or row.membership_id == membership_id
        )


def actor() -> AuthenticatedActor:
    return AuthenticatedActor(
        uuid4(),
        "synthetic@example.test",
        "Synthetic",
        uuid4(),
        uuid4(),
        "Synthetic org",
        MembershipRole.MANAGER,
    )


def reader(source: Source, cache: MemoryCache, *, now: datetime | None = None) -> Any:
    from app.modules.people_capacity.application.workload_cache import WorkloadCacheReader

    return WorkloadCacheReader(
        transactions=source,
        cache=cache,
        namespace="test:unit",
        clock=lambda: now or datetime(2026, 11, 12, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_workload_cache_preserves_values_and_avoids_source_reload() -> None:
    source, cache = Source(actor()), MemoryCache()
    cached = reader(source, cache)
    first = await cached.read(actor=source.actor, week_start=date(2026, 11, 9), membership_id=None)
    second = await cached.read(actor=source.actor, week_start=date(2026, 11, 9), membership_id=None)
    assert first == second == source.values
    assert source.loads == 1
    assert len(cache.values) == 1


@pytest.mark.asyncio
async def test_revision_change_never_uses_previous_workload() -> None:
    source, cache = Source(actor()), MemoryCache()
    cached = reader(source, cache)
    await cached.read(actor=source.actor, week_start=date(2026, 11, 9), membership_id=None)
    source.revision = replace(source.revision, generation=2)
    source.values = (
        replace(
            source.values[0],
            allocated_effort_hours=6,
            residual_capacity_hours=2,
            workload_ratio=Decimal("0.75"),
        ),
    )
    result = await cached.read(actor=source.actor, week_start=date(2026, 11, 9), membership_id=None)
    assert result[0].allocated_effort_hours == 6
    assert source.loads == 2


@pytest.mark.asyncio
async def test_cross_tenant_cache_envelope_cannot_be_consumed() -> None:
    cache = MemoryCache()
    first, other = Source(actor()), Source(actor())
    cached = reader(first, cache)
    await cached.read(actor=first.actor, week_start=date(2026, 11, 9), membership_id=None)
    first_key = next(iter(cache.values))
    await reader(other, cache).read(
        actor=other.actor, week_start=date(2026, 11, 9), membership_id=None
    )
    other_key = next(key for key in cache.values if key != first_key)
    cache.values[first_key] = cache.values[other_key]
    result = await cached.read(actor=first.actor, week_start=date(2026, 11, 9), membership_id=None)
    assert result == first.values
    assert first.loads == 2


@pytest.mark.asyncio
async def test_fresh_employee_role_cannot_consume_manager_cached_scope() -> None:
    original = actor()
    source, cache = Source(original), MemoryCache()
    cached = reader(source, cache)
    await cached.read(actor=original, week_start=date(2026, 11, 9), membership_id=None)
    source.actor = replace(original, role=MembershipRole.EMPLOYEE)
    with pytest.raises(PeopleSkillForbiddenError):
        await cached.read(actor=original, week_start=date(2026, 11, 9), membership_id=uuid4())


@pytest.mark.asyncio
async def test_expired_or_malformed_cache_falls_back_to_source() -> None:
    now = datetime(2026, 11, 12, tzinfo=UTC)
    source, cache = Source(actor()), MemoryCache()
    await reader(source, cache, now=now).read(
        actor=source.actor, week_start=date(2026, 11, 9), membership_id=None
    )
    later = reader(source, cache, now=now + timedelta(seconds=61))
    assert await later.read(actor=source.actor, week_start=date(2026, 11, 9), membership_id=None)
    key = next(iter(cache.values))
    cache.values[key] = b"invalid JSON"
    assert await later.read(actor=source.actor, week_start=date(2026, 11, 9), membership_id=None)
    assert source.loads == 3


@pytest.mark.asyncio
async def test_missing_revision_and_oversize_results_are_not_cached() -> None:
    source, cache = Source(actor()), MemoryCache()
    source.revision = None
    assert (
        await reader(source, cache).read(
            actor=source.actor, week_start=date(2026, 11, 9), membership_id=None
        )
        == source.values
    )
    assert cache.values == {}


@pytest.mark.asyncio
async def test_oversize_workload_is_returned_without_cache_storage() -> None:
    source, cache = Source(actor()), MemoryCache()
    source.values *= 10_001
    result = await reader(source, cache).read(
        actor=source.actor, week_start=date(2026, 11, 9), membership_id=None
    )
    assert len(result) == 10_001
    assert cache.values == {}


@pytest.mark.asyncio
async def test_slow_cache_total_time_budget_is_bounded() -> None:
    class HangingCache(MemoryCache):
        async def get(self, key: str) -> bytes | None:
            await asyncio.sleep(10)
            return None

        async def put(self, key: str, payload: bytes, *, ttl_ms: int) -> bool:
            raise AssertionError("exhausted cache budget must not retry a write")

    source = Source(actor())
    async with asyncio.timeout(0.2):
        result = await reader(source, HangingCache()).read(
            actor=source.actor, week_start=date(2026, 11, 9), membership_id=None
        )
    assert result == source.values
