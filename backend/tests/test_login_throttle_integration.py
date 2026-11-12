"""Actual cookie login limits keep SQL session authority and manual outage access."""

import asyncio
import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text

from app.core.config import Settings
from app.main import create_app
from app.modules.optimization.adapters.redis_runtime import create_redis_runtime
from app.modules.optimization.application.login_throttle import LoginThrottle
from app.scripts.benchmark_redis import fixture_id, validate_target

pytestmark = [
    pytest.mark.integration,
    pytest.mark.redis_integration,
    pytest.mark.skipif(
        os.getenv("RUN_REDIS_INTEGRATION") != "1" or os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
        reason="requires owned SQL/Redis",
    ),
]
SECRET = "synthetic-login-hash-key-for-tests-only-32"


def application(*, outage: bool = False):
    target = validate_target(
        os.environ["APP_DATABASE_URL"], os.environ["REDIS_BENCHMARK_INSTANCE_TOKEN"]
    )
    return create_app(
        Settings(
            environment="test",
            database_url=target.database_url.get_secret_value(),
            local_auth_organization_slug="redis-bench-0",
            ai_provider="disabled",
            langsmith_tracing=False,
            login_throttle_enabled=True,
            rate_limit_hash_key=SecretStr(SECRET),
            redis_namespace="login-" + uuid4().hex[:20],
            redis_url=SecretStr(
                "redis://workcrew@127.0.0.1:1/0" if outage else os.environ["REDIS_TEST_URL"]
            ),
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("email", ["bench-0-0@example.test", "unknown-login-account@example.test"])
async def test_known_and_unknown_account_11th_attempt_have_the_same_public_error(email: str):
    app = application()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app, client=("203.0.113.10", 1234)), base_url="http://test"
        ) as client,
    ):
        for _ in range(10):
            result = await client.post(
                "/api/v1/auth/login", json={"email": email, "password": "incorrect"}
            )
            assert result.status_code == 401
        denied = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": "incorrect"}
        )
        assert denied.status_code == 429
        assert denied.json()["error"]["code"] == "LOGIN_RATE_LIMITED"
        assert 1 <= int(denied.headers["Retry-After"]) <= 60
        assert "set-cookie" not in denied.headers
        async with app.state.database_engine.connect() as sql:
            row = (
                await sql.execute(
                    text(
                        "SELECT actor_membership_id,reason_data FROM audit_events "
                        "WHERE request_id=:id"
                    ),
                    {"id": denied.json()["error"]["request_id"]},
                )
            ).one()
            assert row.actor_membership_id is None
            assert row.reason_data["code"] == "LOGIN_RATE_LIMITED"


@pytest.mark.asyncio
async def test_untrusted_forwarded_for_is_ignored():
    target = validate_target(
        os.environ["APP_DATABASE_URL"], os.environ["REDIS_BENCHMARK_INSTANCE_TOKEN"]
    )
    app = application()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app, client=("203.0.113.10", 1234)), base_url="http://test"
        ) as client,
    ):
        runtime = app.state.redis_runtime
        throttle = LoginThrottle(
            runtime.limiter, namespace=runtime.namespace, hash_key=SECRET.encode()
        )
        for index in range(60):
            assert (
                await throttle.check(
                    trusted_organization_id=fixture_id(target, "org", 0),
                    normalized_account=f"synthetic{index}@example.test",
                    client_ip="203.0.113.10",
                )
            ).allowed
        denied = await client.post(
            "/api/v1/auth/login",
            json={"email": "bench-0-0@example.test", "password": "RedisBenchmarkOnly-2026!"},
            headers={"X-Forwarded-For": "8.8.8.8", "Forwarded": "for=8.8.4.4"},
        )
        assert denied.status_code == 429
        assert "set-cookie" not in denied.headers


@pytest.mark.asyncio
async def test_login_outage_uses_bounded_local_fallback_and_keeps_logout_authority():
    app = application(outage=True)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        accepted = await client.post(
            "/api/v1/auth/login",
            json={"email": "bench-0-0@example.test", "password": "RedisBenchmarkOnly-2026!"},
        )
        assert accepted.status_code == 200
        assert (await client.get("/api/v1/projects")).status_code == 200
        assert (await client.post("/api/v1/auth/logout")).status_code == 204
        assert (await client.get("/api/v1/me")).status_code == 401


@pytest.mark.asyncio
async def test_account_limit_is_shared_across_concurrent_pools():
    namespace = "login-race-" + uuid4().hex[:18]
    settings = Settings(
        environment="test",
        login_throttle_enabled=True,
        rate_limit_hash_key=SecretStr(SECRET),
        redis_namespace=namespace,
        redis_url=SecretStr(os.environ["REDIS_TEST_URL"]),
    )
    runtimes = [create_redis_runtime(settings), create_redis_runtime(settings)]
    assert all(runtime.limiter is not None for runtime in runtimes)
    throttles = [
        LoginThrottle(runtime.limiter, namespace=runtime.namespace, hash_key=SECRET.encode())
        for runtime in runtimes
        if runtime.limiter is not None
    ]
    org = uuid4()
    semaphore = asyncio.Semaphore(10)

    async def check(index: int):
        async with semaphore:
            return await throttles[index % 2].check(
                trusted_organization_id=org,
                normalized_account="synthetic@example.test",
                client_ip="203.0.113.10",
            )

    try:
        results = await asyncio.gather(*(check(i) for i in range(30)))
        assert sum(result.allowed for result in results) == 10
        assert all(throttle.degraded_checks == 0 for throttle in throttles)
    finally:
        for runtime in runtimes:
            await runtime.aclose()
