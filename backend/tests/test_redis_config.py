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
