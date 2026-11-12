"""Redis protocol/ACL boundaries, exercised only on an explicit test instance."""

from __future__ import annotations

import os
from typing import Protocol, cast
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.core.config import Settings


class _RedisCommandProbe(Protocol):
    async def execute_command(self, *args: str) -> object: ...


pytestmark = [
    pytest.mark.integration,
    pytest.mark.redis_integration,
    pytest.mark.skipif(
        os.getenv("RUN_REDIS_INTEGRATION") != "1",
        reason="requires task-owned Redis",
    ),
]


@pytest.mark.asyncio
async def test_redis_cache_roundtrip_scope_and_acl_denial() -> None:
    from redis.exceptions import ResponseError

    from app.modules.optimization.adapters.redis_runtime import create_redis_runtime

    runtime = create_redis_runtime(
        Settings(
            environment="test",
            read_cache_enabled=True,
            read_cache_families=["workload.weekly"],
            redis_url=SecretStr(os.environ["REDIS_TEST_URL"]),
            redis_namespace="it-" + uuid4().hex[:24],
        )
    )
    try:
        key = f"wm:{runtime.namespace}:redis-v1:org:{uuid4()}:test"
        assert await runtime.read_cache.put(key, b"test-payload", ttl_ms=1_000)
        assert await runtime.read_cache.get(key) == b"test-payload"
        assert await runtime.read_cache.get(key + "-other") is None
        assert runtime.client is not None
        probe = cast(_RedisCommandProbe, runtime.client)
        with pytest.raises(ResponseError):
            await probe.execute_command("KEYS", "*")
        with pytest.raises(ResponseError):
            await probe.execute_command("CONFIG", "GET", "maxmemory")
        with pytest.raises(ResponseError):
            await runtime.client.set("outside-application-namespace", b"denied")
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_redis_cache_unavailable_is_a_bounded_miss() -> None:
    from app.modules.optimization.adapters.redis_runtime import create_redis_runtime

    runtime = create_redis_runtime(
        Settings(
            environment="test",
            read_cache_enabled=True,
            read_cache_families=["workload.weekly"],
            redis_url=SecretStr("redis://workcrew@127.0.0.1:1/0"),
        )
    )
    try:
        key = f"wm:{runtime.namespace}:redis-v1:org:{uuid4()}:missing"
        assert await runtime.read_cache.get(key) is None
        assert not await runtime.read_cache.put(key, b"data", ttl_ms=1_000)
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_exhausted_pool_falls_back_and_connections_can_be_reused() -> None:
    import asyncio

    from app.modules.optimization.adapters.redis_runtime import create_redis_runtime

    runtime = create_redis_runtime(
        Settings(
            environment="test",
            read_cache_enabled=True,
            redis_url=SecretStr(os.environ["REDIS_TEST_URL"]),
            redis_namespace="pool-" + uuid4().hex[:20],
        )
    )
    assert runtime.client is not None
    pool = runtime.client.connection_pool
    held = [await pool.get_connection() for _ in range(20)]
    try:
        key = f"wm:{runtime.namespace}:redis-v1:pool-test"
        async with asyncio.timeout(0.1):
            assert await runtime.read_cache.get(key) is None
        await pool.release(held.pop())
        assert await runtime.read_cache.put(key, b"reused", ttl_ms=1000)
        assert await runtime.read_cache.get(key) == b"reused"
    finally:
        for connection in held:
            await pool.release(connection)
        await runtime.aclose()
