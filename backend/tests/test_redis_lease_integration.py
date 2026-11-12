"""Owner-token comparisons are verified against real Redis Lua semantics."""

import os
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.core.config import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.redis_integration,
    pytest.mark.skipif(os.getenv("RUN_REDIS_INTEGRATION") != "1", reason="requires owned Redis"),
]


@pytest.mark.asyncio
async def test_old_owner_cannot_release_new_lease() -> None:
    from app.modules.optimization.adapters.redis_runtime import create_redis_runtime

    runtime = create_redis_runtime(
        Settings(
            environment="test",
            read_cache_enabled=True,
            redis_url=SecretStr(os.environ["REDIS_TEST_URL"]),
            redis_namespace="lease-" + uuid4().hex[:20],
        )
    )
    try:
        assert runtime.leases is not None
        key = f"wm:{runtime.namespace}:redis-v1:org:{uuid4()}:lease"
        assert await runtime.leases.try_acquire(key, "a" * 32, lease_ms=5000) is True
        assert await runtime.leases.try_acquire(key, "b" * 32, lease_ms=5000) is False
        assert not await runtime.leases.release_if_owner(key, "b" * 32)
        assert await runtime.leases.release_if_owner(key, "a" * 32)
        assert await runtime.leases.try_acquire(key, "b" * 32, lease_ms=5000) is True
        assert not await runtime.leases.release_if_owner(key, "a" * 32)
        assert await runtime.leases.release_if_owner(key, "b" * 32)
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("REDIS_OOM_TEST_URL"), reason="requires separate 1MB noeviction Redis"
)
async def test_oom_is_a_cache_miss_and_never_reported_as_held_lease() -> None:
    from app.modules.optimization.adapters.redis_runtime import create_redis_runtime

    runtime = create_redis_runtime(
        Settings(
            environment="test",
            read_cache_enabled=True,
            redis_url=SecretStr(os.environ["REDIS_OOM_TEST_URL"]),
            redis_namespace="oom-" + uuid4().hex[:20],
        )
    )
    try:
        assert runtime.leases is not None
        key = f"wm:{runtime.namespace}:redis-v1:org:{uuid4()}:oom"
        assert not await runtime.read_cache.put(key, b"x" * (256 * 1024), ttl_ms=1000)
        assert await runtime.read_cache.get(key) is None
        assert await runtime.leases.try_acquire(key + ":lease", "c" * 32, lease_ms=5000) is None
    finally:
        await runtime.aclose()
