"""Opt-in benchmark fixture and actual authenticated ASGI path verification."""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.main import create_app
from app.scripts.benchmark_redis import (
    BenchmarkSafetyError,
    FixtureSpec,
    assert_owned_database,
    fixture_id,
    run_benchmark,
    setup_fixture,
    validate_target,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_REDIS_BENCHMARK_INTEGRATION") != "1",
        reason="requires explicitly owned benchmark PostgreSQL",
    ),
]


@pytest.mark.asyncio
async def test_owned_fixture_and_real_asgi_benchmark_preserve_actor_scope() -> None:
    target = validate_target(
        os.environ["APP_DATABASE_URL"], os.environ["REDIS_BENCHMARK_INSTANCE_TOKEN"]
    )
    engine = create_async_engine(target.database_url.get_secret_value())
    fixture = FixtureSpec()
    try:
        await setup_fixture(engine, target, fixture, allowed=True)
        await setup_fixture(engine, target, fixture, allowed=True)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM organizations")) == 2
            assert await connection.scalar(text("SELECT count(*) FROM memberships")) == 200
            assert await connection.scalar(text("SELECT count(*) FROM tasks")) == 10_000
        results = await run_benchmark(
            target,
            fixture,
            cases=["workload.person"],
            concurrencies=[2],
            duration_seconds=0.1,
            repetitions=1,
        )
        assert results[0]["errors"] == 0
        assert results[0]["samples"] >= 2
        assert results[0]["sql_statements_per_request"] > 0
        assert results[0]["sql_ms_per_request"] > 0
        mixed = await run_benchmark(
            target,
            fixture,
            cases=["workload.mixed"],
            concurrencies=[2],
            duration_seconds=1,
            repetitions=1,
        )
        assert mixed[0]["errors"] == 0
        assert mixed[0]["writes"]["samples"] > 0
        assert mixed[0]["reads"]["samples"] > mixed[0]["writes"]["samples"]
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM capacity_entries WHERE hours != 40")
                )
                == 0
            )

        app = create_app(
            Settings(
                environment="test",
                database_url=target.database_url.get_secret_value(),
                local_auth_organization_slug="redis-bench-0",
                ai_provider="disabled",
                langsmith_tracing=False,
            )
        )
        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app), base_url="http://benchmark.test"
            ) as client,
        ):
            login = await client.post(
                "/api/v1/auth/login",
                json={
                    "email": "bench-0-1@example.test",
                    "password": "RedisBenchmarkOnly-2026!",
                },
            )
            assert login.status_code == 200
            own = await client.get("/api/v1/workload?week_start=2026-11-09")
            assert own.status_code == 200
            assert len(own.json()) == 50
            assert {row["membership_id"] for row in own.json()} == {
                str(fixture_id(target, "member", 0, 1)),
            }
            for tenant, member in [(0, 2), (1, 1)]:
                other = fixture_id(target, "member", tenant, member)
                denied = await client.get(
                    f"/api/v1/workload?week_start=2026-11-09&membership_id={other}"
                )
                assert denied.status_code == 403
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_forged_ownership_marker_is_rejected_before_seed() -> None:
    from dataclasses import replace

    from pydantic import SecretStr

    target = validate_target(
        os.environ["APP_DATABASE_URL"], os.environ["REDIS_BENCHMARK_INSTANCE_TOKEN"]
    )
    engine = create_async_engine(target.database_url.get_secret_value())
    try:
        forged = replace(target, instance_token=SecretStr("f" * 32))
        with pytest.raises(BenchmarkSafetyError, match="DATABASE_OWNERSHIP_NOT_VERIFIED"):
            await assert_owned_database(engine, forged)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_changed_fixture_rows_are_rejected_without_reset() -> None:
    from app.scripts.benchmark_redis import assert_fixture

    target = validate_target(
        os.environ["APP_DATABASE_URL"], os.environ["REDIS_BENCHMARK_INSTANCE_TOKEN"]
    )
    engine = create_async_engine(target.database_url.get_secret_value())
    capacity = fixture_id(target, "capacity", 0, 1)
    try:
        await setup_fixture(engine, target, FixtureSpec(), allowed=True)
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE capacity_entries SET hours=41 WHERE id=:id"), {"id": capacity}
            )
        with pytest.raises(BenchmarkSafetyError, match="FIXTURE_INPUTS_CHANGED"):
            await assert_fixture(engine, target, FixtureSpec())
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE capacity_entries SET hours=40 WHERE id=:id"), {"id": capacity}
            )
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("RUN_REDIS_INTEGRATION") != "1", reason="requires Redis too")
async def test_benchmark_compares_direct_and_cache_with_auth_cost_included() -> None:
    from pydantic import SecretStr

    target = validate_target(
        os.environ["APP_DATABASE_URL"], os.environ["REDIS_BENCHMARK_INSTANCE_TOKEN"]
    )
    direct = await run_benchmark(
        target,
        FixtureSpec(),
        cases=["workload.person"],
        concurrencies=[2],
        duration_seconds=0.2,
        repetitions=1,
    )
    cached = await run_benchmark(
        target,
        FixtureSpec(),
        cases=["workload.person"],
        concurrencies=[2],
        duration_seconds=0.2,
        repetitions=1,
        cache_url=SecretStr(os.environ["REDIS_TEST_URL"]),
    )
    assert direct[0]["errors"] == cached[0]["errors"] == 0
    assert direct[0]["sql_statements_per_request"] == 15
    assert cached[0]["sql_statements_per_request"] <= 7
