"""Actual session-authorized workload reads must invalidate after API writes."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.core.config import Settings
from app.main import create_app
from app.scripts.benchmark_redis import fixture_id, instrument_sql, measure_request, validate_target

pytestmark = [
    pytest.mark.integration,
    pytest.mark.redis_integration,
    pytest.mark.skipif(
        os.getenv("RUN_REDIS_INTEGRATION") != "1",
        reason="requires owned Redis/PostgreSQL",
    ),
]


@pytest.mark.asyncio
async def test_workload_cache_hit_then_committed_capacity_change_is_fresh() -> None:
    target = validate_target(
        os.environ["APP_DATABASE_URL"], os.environ["REDIS_BENCHMARK_INSTANCE_TOKEN"]
    )
    settings = Settings(
        environment="test",
        database_url=target.database_url.get_secret_value(),
        local_auth_organization_slug="redis-bench-0",
        ai_provider="disabled",
        langsmith_tracing=False,
        read_cache_enabled=True,
        read_cache_families=["workload.weekly"],
        redis_url=SecretStr(os.environ["REDIS_TEST_URL"]),
        redis_namespace="it-" + uuid4().hex[:24],
    )
    app = create_app(settings)
    instrument_sql(app.state.database_engine)
    member = str(fixture_id(target, "member", 0, 1))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://benchmark.test",
        ) as client,
    ):
        assert (
            await client.post(
                "/api/v1/auth/login",
                json={"email": "bench-0-0@example.test", "password": "RedisBenchmarkOnly-2026!"},
            )
        ).status_code == 200
        path = f"/api/v1/workload?week_start=2026-11-09&membership_id={member}"
        cold = await measure_request(client, path)
        warm = await measure_request(client, path)
        assert cold.status == warm.status == 200
        assert warm.sql_statements <= 7
        assert cold.sql_statements > warm.sql_statements
        before = await client.get(path)
        assert before.status_code == 200
        assert app.state.redis_runtime.client is not None
        current = (await client.get(f"/api/v1/capacity?membership_id={member}")).json()[0]
        payload = {
            "membership_id": member,
            "kind": "DEFAULT",
            "hours": 39,
            "effective_from": "2026-11-09",
            "effective_to": "2027-11-08",
        }
        changed = await client.post(
            "/api/v1/capacity",
            json=payload,
            headers={"Idempotency-Key": uuid4().hex, "If-Match": f'"{current["version"]}"'},
        )
        assert changed.status_code == 200
        try:
            fresh = await client.get(path)
            assert fresh.status_code == 200
            assert {row["effective_capacity_hours"] for row in fresh.json()} == {39}
            foreign = str(fixture_id(target, "member", 1, 1))
            denied = await client.get(
                f"/api/v1/workload?week_start=2026-11-09&membership_id={foreign}"
            )
            assert denied.status_code == 404
        finally:
            restored = await client.post(
                "/api/v1/capacity",
                json={**payload, "hours": 40},
                headers={
                    "Idempotency-Key": uuid4().hex,
                    "If-Match": f'"{changed.json()["version"]}"',
                },
            )
            assert restored.status_code == 200
