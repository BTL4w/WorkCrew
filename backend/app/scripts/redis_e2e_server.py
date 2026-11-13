"""Start a mock-provider browser lane only after verifying its task-owned DB."""

import asyncio
import os
from urllib.parse import urlsplit
from uuid import UUID

import uvicorn
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.main import create_app
from app.modules.optimization.adapters.redis_runtime import create_redis_runtime
from app.scripts.benchmark_redis import FixtureSpec, assert_fixture, validate_target


async def verify() -> Settings:
    target = validate_target(
        os.environ.get("REDIS_E2E_DATABASE_URL", ""),
        os.environ.get("REDIS_BENCHMARK_INSTANCE_TOKEN", ""),
    )
    engine = create_async_engine(target.database_url.get_secret_value())
    try:
        await assert_fixture(engine, target, FixtureSpec())
    finally:
        await engine.dispose()
    return Settings(
        environment="test",
        database_url=target.database_url.get_secret_value(),
        local_auth_organization_slug="redis-bench-0",
        ai_provider="mock" if os.environ.get("REDIS_E2E_ADMISSION") == "1" else "disabled",
        langsmith_tracing=False,
        frontend_origin="http://127.0.0.1:3117",
        read_cache_enabled=True,
        read_cache_families=["workload.weekly"],
        redis_url=SecretStr(os.environ["REDIS_TEST_URL"]),
        redis_namespace=os.environ.get("REDIS_E2E_NAMESPACE", "browser-test"),
        redis_wakeup_enabled=(
            os.environ.get("REDIS_E2E_WAKEUP") == "1"
            and os.environ.get("REDIS_E2E_WAKEUP_MODE") != "disabled"
        ),
        ai_admission_enabled=os.environ.get("REDIS_E2E_ADMISSION") == "1",
        ai_admission_actor_limit=1 if os.environ.get("REDIS_E2E_ADMISSION") == "1" else 20,
        login_throttle_enabled=os.environ.get("REDIS_E2E_LOGIN") == "1",
        rate_limit_hash_key=SecretStr(os.environ.get("REDIS_E2E_HASH_KEY", "")),
    )


async def fill_test_login_ip() -> None:
    from app.modules.optimization.application.login_throttle import LoginThrottle
    from app.scripts.benchmark_redis import fixture_id

    settings = await verify()
    redis_url = urlsplit(os.environ["REDIS_TEST_URL"])
    if (
        not settings.login_throttle_enabled
        or not settings.redis_namespace.startswith("browser-login-")
        or redis_url.hostname not in {"localhost", "127.0.0.1", "::1"}
        or redis_url.port in {None, 6379}
    ):
        raise ValueError("DEDICATED_LOGIN_LANE_REQUIRED")
    target = validate_target(
        os.environ["REDIS_E2E_DATABASE_URL"], os.environ["REDIS_BENCHMARK_INSTANCE_TOKEN"]
    )
    runtime = create_redis_runtime(settings)
    try:
        assert runtime.limiter is not None and settings.rate_limit_hash_key is not None
        throttle = LoginThrottle(
            runtime.limiter,
            namespace=runtime.namespace,
            hash_key=settings.rate_limit_hash_key.get_secret_value().encode(),
        )
        for index in range(60):
            await throttle.check(
                trusted_organization_id=fixture_id(target, "org", 0),
                normalized_account=f"ipfixture{index}@example.test",
                client_ip="127.0.0.1",
            )
    finally:
        await runtime.aclose()


async def reset_test_limits(organization_id: UUID, membership_id: UUID) -> None:
    """Expire two exact synthetic counters; no product control/reset API exists."""
    from app.scripts.benchmark_redis import fixture_id

    settings = await verify()
    if not settings.ai_admission_enabled or not settings.redis_namespace.startswith("browser-adm-"):
        raise ValueError("DEDICATED_ADMISSION_LANE_REQUIRED")
    redis_url = urlsplit(os.environ["REDIS_TEST_URL"])
    if (
        redis_url.scheme != "redis"
        or redis_url.hostname not in {"127.0.0.1", "localhost", "::1"}
        or redis_url.port in {None, 6379}
    ):
        raise ValueError("DEDICATED_REDIS_REQUIRED")
    target = validate_target(
        os.environ["REDIS_E2E_DATABASE_URL"], os.environ["REDIS_BENCHMARK_INSTANCE_TOKEN"]
    )
    if organization_id != fixture_id(target, "org", 0) or membership_id not in {
        fixture_id(target, "member", 0, 2),
        fixture_id(target, "member", 0, 3),
    }:
        raise ValueError("SYNTHETIC_ACTOR_REQUIRED")
    runtime = create_redis_runtime(settings)
    try:
        assert runtime.client is not None
        prefix = f"wm:{runtime.namespace}:limit:ai:org:{organization_id}"
        await runtime.client.delete(prefix, f"{prefix}:actor:{membership_id}")
    finally:
        await runtime.aclose()


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 4 and sys.argv[1] == "reset-test-limits":
        asyncio.run(reset_test_limits(UUID(sys.argv[2]), UUID(sys.argv[3])))
    elif len(sys.argv) == 2 and sys.argv[1] == "fill-test-login-ip":
        asyncio.run(fill_test_login_ip())
    elif len(sys.argv) == 1:
        uvicorn.run(
            create_app(asyncio.run(verify())), host="127.0.0.1", port=8117, proxy_headers=False
        )
    else:
        raise SystemExit("INVALID_TEST_COMMAND")
