"""Cache failures and same-key bursts must not hold source transactions open."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import date
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.optimization.adapters.redis_cache import RedisReadCache
from app.modules.people_capacity.application.workload_cache import WorkloadReadSnapshot
from app.modules.people_capacity.domain.workload import WeeklyWorkload


@pytest.mark.asyncio
async def test_breaker_skips_network_after_three_failures_and_recovers_with_one_probe() -> None:
    class BrokenConnection:
        calls = 0
        healthy = False

        async def get(self, key: str) -> bytes:
            self.calls += 1
            if not self.healthy:
                raise RedisConnectionError("synthetic connection failure")
            return b"recovered"

    now = [10.0]
    connection = BrokenConnection()
    cache = RedisReadCache(cast(Redis, connection), "test:breaker", clock=lambda: now[0])
    key = "wm:test:breaker:redis-v1:entry"
    for _ in range(4):
        assert await cache.get(key) is None
    assert connection.calls == 3
    now[0] = 15.1
    connection.healthy = True
    assert await cache.get(key) == b"recovered"
    assert connection.calls == 4


@pytest.mark.asyncio
async def test_cancelled_half_open_probe_does_not_leave_breaker_stuck() -> None:
    entered, ready = asyncio.Event(), asyncio.Event()

    class Connection:
        calls = 0

        async def get(self, key: str) -> bytes:
            self.calls += 1
            if self.calls <= 3:
                raise RedisConnectionError("synthetic failure")
            entered.set()
            await ready.wait()
            return b"healthy"

    connection = Connection()
    now = [0.0]
    cache = RedisReadCache(cast(Redis, connection), "test:probe", clock=lambda: now[0])
    key = "wm:test:probe:redis-v1:entry"
    for _ in range(3):
        assert await cache.get(key) is None
    now[0] = 6
    probe = asyncio.create_task(cache.get(key))
    await entered.wait()
    assert await cache.get(key) is None
    assert connection.calls == 4
    probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe
    ready.set()
    assert await cache.get(key) == b"healthy"
    assert connection.calls == 5


@pytest.mark.asyncio
async def test_command_finishing_after_trip_cannot_close_the_breaker() -> None:
    entered, ready = asyncio.Event(), asyncio.Event()

    class Connection:
        calls = 0

        async def get(self, key: str) -> bytes:
            self.calls += 1
            if self.calls == 1:
                entered.set()
                await ready.wait()
                return b"old-success"
            raise RedisConnectionError("synthetic failure")

    connection = Connection()
    cache = RedisReadCache(cast(Redis, connection), "test:epoch")
    key = "wm:test:epoch:redis-v1:entry"
    early = asyncio.create_task(cache.get(key))
    await entered.wait()
    for _ in range(3):
        assert await cache.get(key) is None
    ready.set()
    assert await early == b"old-success"
    assert await cache.get(key) is None
    assert connection.calls == 4


@pytest.mark.asyncio
async def test_outer_workload_budget_exhaustion_trips_cache_breaker() -> None:
    from app.modules.people_capacity.application.workload_cache import WorkloadCacheReader
    from tests.test_workload_cache import Source, actor

    class Connection:
        calls = 0

        async def get(self, key: str) -> bytes:
            self.calls += 1
            await asyncio.sleep(10)
            return b"unreachable"

    source = Source(actor())
    connection = Connection()
    cache = RedisReadCache(cast(Redis, connection), "test:budget")
    reader = WorkloadCacheReader(transactions=source, cache=cache, namespace="test:budget")
    for _ in range(4):
        assert (
            await reader.read(actor=source.actor, week_start=date(2026, 11, 9), membership_id=None)
            == source.values
        )
    assert connection.calls == 3


@pytest.mark.asyncio
async def test_tripped_cache_breaker_also_bypasses_lease_connections() -> None:
    from app.modules.optimization.adapters.redis_lease import RedisCacheLeases

    class Connection:
        calls = 0

        async def get(self, key: str) -> bytes:
            self.calls += 1
            raise RedisConnectionError("synthetic failure")

        async def set(self, *args: object, **kwargs: object) -> bool:
            self.calls += 1
            raise RedisConnectionError("synthetic failure")

    connection = Connection()
    cache = RedisReadCache(cast(Redis, connection), "test:shared")
    leases = RedisCacheLeases(cast(Redis, connection), "test:shared", available=cache.allow_leases)
    key = "wm:test:shared:redis-v1:entry"
    for _ in range(3):
        assert await cache.get(key) is None
    assert await leases.try_acquire(key + ":lease", "a" * 32, lease_ms=5000) is None
    assert connection.calls == 3


@pytest.mark.asyncio
async def test_single_flight_waits_outside_snapshot_and_reads_new_revision() -> None:
    from contextlib import asynccontextmanager
    from dataclasses import replace

    from app.modules.optimization.application.single_flight import SingleFlight
    from app.modules.people_capacity.application.workload_cache import WorkloadCacheReader
    from tests.test_workload_cache import MemoryCache, Source, actor

    source = Source(actor())
    entered = [False]

    @asynccontextmanager
    async def transactions(current: AuthenticatedActor) -> AsyncGenerator[WorkloadReadSnapshot]:
        entered[0] = True
        try:
            yield source
        finally:
            entered[0] = False

    class HeldLease:
        async def try_acquire(self, key: str, owner: str, *, lease_ms: int) -> bool | None:
            return False

        async def release_if_owner(self, key: str, owner: str) -> bool:
            raise AssertionError("waiter owns no lease")

    class ObserveWait(SingleFlight):
        async def wait(self) -> None:
            assert not entered[0]
            source.revision = replace(source.revision, generation=2)
            source.values = (
                replace(
                    source.values[0],
                    allocated_effort_hours=6,
                    residual_capacity_hours=2,
                    workload_ratio=Decimal("0.75"),
                ),
            )

    reader = WorkloadCacheReader(
        transactions=transactions,
        cache=MemoryCache(),
        namespace="test:flight",
        single_flight=ObserveWait(HeldLease()),
    )
    async with asyncio.timeout(0.2):
        result = await reader.read(
            actor=source.actor, week_start=date(2026, 11, 9), membership_id=None
        )
    assert result[0].allocated_effort_hours == 6
    assert source.loads == 1


@pytest.mark.asyncio
async def test_fifty_same_key_requests_only_load_source_once() -> None:

    from app.modules.optimization.application.single_flight import SingleFlight
    from app.modules.people_capacity.application.workload_cache import WorkloadCacheReader
    from tests.test_workload_cache import MemoryCache, Source, actor

    class SlowSource(Source):
        async def load(
            self, *, week_start: date, membership_id: UUID | None
        ) -> tuple[WeeklyWorkload, ...]:
            await asyncio.sleep(0.01)
            return await super().load(week_start=week_start, membership_id=membership_id)

    class Leases:
        owner: str | None = None

        async def try_acquire(self, key: str, owner: str, *, lease_ms: int) -> bool | None:
            if self.owner is not None:
                return False
            self.owner = owner
            return True

        async def release_if_owner(self, key: str, owner: str) -> bool:
            assert self.owner == owner
            self.owner = None
            return True

    source = SlowSource(actor())
    reader = WorkloadCacheReader(
        transactions=source,
        cache=MemoryCache(),
        namespace="test:burst",
        single_flight=SingleFlight(Leases()),
    )
    values = await asyncio.gather(
        *(
            reader.read(actor=source.actor, week_start=date(2026, 11, 9), membership_id=None)
            for _ in range(50)
        )
    )
    assert all(value == source.values for value in values)
    assert source.loads == 1
