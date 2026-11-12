"""Optional cache settings cannot enable network access or override time budgets."""

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings


def test_enabled_cache_requires_an_explicit_redis_url() -> None:
    with pytest.raises(ValidationError):
        Settings(read_cache_enabled=True)


@pytest.mark.parametrize(
    "url",
    [
        "redis://localhost:6379/0?socket_timeout=10",
        "http://localhost:6379",
        "redis://localhost:6379/0#fragment",
    ],
)
def test_redis_url_cannot_override_connection_policy(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(read_cache_enabled=True, redis_url=SecretStr(url))


@pytest.mark.asyncio
async def test_disabled_cache_works_without_a_redis_connection() -> None:
    from app.modules.optimization.adapters.redis_runtime import create_redis_runtime

    runtime = create_redis_runtime(Settings(read_cache_enabled=False))
    try:
        assert runtime.client is None
        assert await runtime.read_cache.get("wm:test:key") is None
        assert not await runtime.read_cache.put("wm:test:key", b"payload", ttl_ms=1_000)
    finally:
        await runtime.aclose()


def test_admission_requires_explicit_url_and_positive_trusted_limits() -> None:
    with pytest.raises(ValidationError):
        Settings(ai_admission_enabled=True)
    with pytest.raises(ValidationError):
        Settings(ai_admission_actor_limit=0)
    assert not Settings().ai_admission_enabled


@pytest.mark.asyncio
async def test_admission_can_run_with_read_cache_disabled() -> None:
    from app.modules.optimization.adapters.redis_runtime import create_redis_runtime

    runtime = create_redis_runtime(
        Settings(ai_admission_enabled=True, redis_url=SecretStr("redis://localhost:1/0"))
    )
    try:
        assert runtime.client is not None and runtime.limiter is not None
        assert runtime.leases is None
        assert await runtime.read_cache.get("wm:test:key") is None
    finally:
        await runtime.aclose()
