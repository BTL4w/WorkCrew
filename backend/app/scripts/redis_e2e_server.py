"""Start a mock-provider browser lane only after verifying its task-owned DB."""

import asyncio
import os

import uvicorn
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.main import create_app
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
        ai_provider="disabled",
        langsmith_tracing=False,
        frontend_origin="http://127.0.0.1:3117",
        read_cache_enabled=True,
        read_cache_families=["workload.weekly"],
        redis_url=SecretStr(os.environ["REDIS_TEST_URL"]),
        redis_namespace="browser-test",
    )


if __name__ == "__main__":
    uvicorn.run(create_app(asyncio.run(verify())), host="127.0.0.1", port=8117)
