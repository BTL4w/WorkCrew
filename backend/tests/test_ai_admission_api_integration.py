"""Real cookie auth and SQL receipts survive rejected or unavailable admission."""

import asyncio
import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text

from app.core.config import Settings
from app.main import create_app
from app.scripts.benchmark_redis import validate_target

pytestmark = [
    pytest.mark.integration,
    pytest.mark.redis_integration,
    pytest.mark.skipif(
        os.getenv("RUN_REDIS_INTEGRATION") != "1", reason="requires owned Redis/SQL"
    ),
]


def application(*, namespace: str, outage: bool = False):
    target = validate_target(
        os.environ["APP_DATABASE_URL"], os.environ["REDIS_BENCHMARK_INSTANCE_TOKEN"]
    )
    return create_app(
        Settings(
            environment="test",
            database_url=target.database_url.get_secret_value(),
            local_auth_organization_slug="redis-bench-0",
            ai_provider="mock",
            langsmith_tracing=False,
            ai_admission_enabled=True,
            redis_namespace=namespace,
            redis_url=SecretStr(
                "redis://workcrew@127.0.0.1:1/0" if outage else os.environ["REDIS_TEST_URL"]
            ),
        )
    )


async def login(client: AsyncClient) -> None:
    result = await client.post(
        "/api/v1/auth/login",
        json={"email": "bench-0-0@example.test", "password": "RedisBenchmarkOnly-2026!"},
    )
    assert result.status_code == 200


@pytest.mark.asyncio
async def test_outage_new_work_vs_durable_replay_and_payload_mismatch() -> None:
    namespace = "api-" + uuid4().hex[:20]
    accepted_app = application(namespace=namespace)
    key = uuid4().hex
    body = {"message": "Summarize my tasks", "locale": "en"}
    async with (
        accepted_app.router.lifespan_context(accepted_app),
        AsyncClient(transport=ASGITransport(app=accepted_app), base_url="http://test") as client,
    ):
        await login(client)
        conversation = await client.post(
            "/api/v1/ai/conversations",
            json={"locale": "en", "title": None},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert conversation.status_code == 201
        cid = conversation.json()["id"]
        path = f"/api/v1/ai/conversations/{cid}/messages"
        first = await client.post(path, json=body, headers={"Idempotency-Key": key})
        assert first.status_code == 202
        receipt = first.json()
    down = application(namespace=namespace, outage=True)
    async with (
        down.router.lifespan_context(down),
        AsyncClient(transport=ASGITransport(app=down), base_url="http://test") as client,
    ):
        await login(client)
        replay = await client.post(path, json=body, headers={"Idempotency-Key": key})
        assert replay.status_code == 202
        assert replay.json() == receipt
        assert replay.headers["Idempotency-Replayed"] == "true"
        mismatch = await client.post(
            path, json={**body, "message": "Different"}, headers={"Idempotency-Key": key}
        )
        assert mismatch.status_code == 409
        rejected = await client.post(path, json=body, headers={"Idempotency-Key": uuid4().hex})
        assert rejected.status_code == 503
        assert rejected.json()["error"]["code"] == "AI_ADMISSION_UNAVAILABLE"
        assert rejected.json()["error"]["details"]["retry_after_seconds"] == 5
        assert rejected.headers["Retry-After"] == "5"
        assert rejected.headers["X-Request-ID"] == rejected.json()["error"]["request_id"]
        assert (await client.get("/api/v1/projects")).status_code == 200
        async with down.state.database_engine.connect() as sql:
            count = await sql.scalar(
                text(
                    "SELECT count(*) FROM assistant_jobs WHERE conversation_id=:id "
                    "AND job_type='assistant.turn.execute'"
                ),
                {"id": cid},
            )
            assert count == 1
            count = await sql.scalar(
                text(
                    "SELECT count(*) FROM audit_events WHERE resource_id=:id "
                    "AND outcome='REJECTED' "
                    "AND reason_data->>'code'='AI_ADMISSION_UNAVAILABLE'"
                ),
                {"id": cid},
            )
            assert count == 1


@pytest.mark.asyncio
async def test_concurrent_same_key_creates_one_job_and_consumes_one_admission() -> None:
    app = application(namespace="race-" + uuid4().hex[:20])
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        await login(client)
        cid = (
            await client.post(
                "/api/v1/ai/conversations",
                json={"locale": "en"},
                headers={"Idempotency-Key": uuid4().hex},
            )
        ).json()["id"]
        key = uuid4().hex
        responses = await asyncio.gather(
            *(
                client.post(
                    f"/api/v1/ai/conversations/{cid}/messages",
                    json={"message": "Summarize my tasks", "locale": "en"},
                    headers={"Idempotency-Key": key},
                )
                for _ in range(8)
            )
        )
        assert {r.status_code for r in responses} == {202}
        assert len({r.json()["turn_id"] for r in responses}) == 1
        me = (await client.get("/api/v1/me")).json()
        runtime = app.state.redis_runtime
        counter = (
            f"wm:{runtime.namespace}:limit:ai:org:{me['membership']['organization_id']}"
            f":actor:{me['membership']['id']}"
        )
        assert (await runtime.client.get(counter)).endswith(b":1")


@pytest.mark.asyncio
async def test_twenty_first_new_turn_is_rejected_with_retry_after() -> None:
    app = application(namespace="bounds-" + uuid4().hex[:20])
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        await login(client)
        redis = app.state.redis_runtime.client
        # Keep the exact-bound assertion inside one real Redis fixed window.
        seconds, _ = await redis.time()
        remaining = 60 - seconds % 60
        if remaining < 10:
            await asyncio.sleep(remaining + 0.01)
        cid = (
            await client.post(
                "/api/v1/ai/conversations",
                json={"locale": "en"},
                headers={"Idempotency-Key": uuid4().hex},
            )
        ).json()["id"]
        path = f"/api/v1/ai/conversations/{cid}/messages"
        for _ in range(20):
            accepted = await client.post(
                path,
                json={"message": "Summarize my tasks", "locale": "en"},
                headers={"Idempotency-Key": uuid4().hex},
            )
            assert accepted.status_code == 202
        rejected = await client.post(
            path,
            json={"message": "Summarize my tasks", "locale": "en"},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert rejected.status_code == 429
        assert rejected.json()["error"]["code"] == "RATE_LIMITED"
        assert 1 <= int(rejected.headers["Retry-After"]) <= 60
        assert (
            int(rejected.headers["Retry-After"])
            == rejected.json()["error"]["details"]["retry_after_seconds"]
        )
        async with app.state.database_engine.connect() as sql:
            assert (
                await sql.scalar(
                    text(
                        "SELECT count(*) FROM assistant_jobs WHERE conversation_id=:id "
                        "AND job_type='assistant.turn.execute'"
                    ),
                    {"id": cid},
                )
                == 20
            )


@pytest.mark.asyncio
async def test_unauthorized_conversation_does_not_reach_unavailable_admission() -> None:
    app = application(namespace="denial-" + uuid4().hex[:20], outage=True)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        await login(client)
        cid = (
            await client.post(
                "/api/v1/ai/conversations",
                json={"locale": "en"},
                headers={"Idempotency-Key": uuid4().hex},
            )
        ).json()["id"]
        assert (
            await client.post(
                "/api/v1/auth/login",
                json={"email": "bench-0-1@example.test", "password": "RedisBenchmarkOnly-2026!"},
            )
        ).status_code == 200
        for resource in (cid, str(uuid4())):
            denied = await client.post(
                f"/api/v1/ai/conversations/{resource}/messages",
                json={"message": "New task", "locale": "en"},
                headers={"Idempotency-Key": uuid4().hex},
            )
            assert denied.status_code == 404


@pytest.mark.asyncio
async def test_new_planning_run_does_not_queue_work_during_outage() -> None:
    app = application(namespace="plan-" + uuid4().hex[:20], outage=True)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        await login(client)
        rejected = await client.post(
            "/api/v1/ai/planning-runs",
            json={"message": "Plan an event for next week", "locale": "en"},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert rejected.status_code == 503
        assert rejected.json()["error"]["code"] == "AI_ADMISSION_UNAVAILABLE"
