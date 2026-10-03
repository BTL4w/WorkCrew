"""Real PostgreSQL blocker lifecycle and report transaction contracts."""

import os
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.progress.adapters.blocker_repository import SqlAlchemyBlockerTransactions
from app.modules.progress.api.blocker_routes import router
from app.modules.progress.application.blocker_service import BlockerService
from tests.test_daily_update_api_integration import body, confirm, daily_app, draft, seed_task
from tests.test_evidence_api_integration import Harness, harness

__all__ = ["harness"]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


def blocker_app(h: Harness) -> FastAPI:
    app = daily_app(h)
    sessions = async_sessionmaker(
        bind=h.connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    app.state.blocker_service = BlockerService(SqlAlchemyBlockerTransactions(sessions))
    app.include_router(router, prefix="/api/v1")
    return app


async def apply(
    c: AsyncClient,
    task: str,
    action: str = "CREATE",
    previous: dict[str, Any] | None = None,
    **changes: Any,
):
    command = {
        "task_id": task,
        "expected_task_version": 1,
        "action": action,
        "text": "Awaiting materials",
        "severity": "HIGH",
        **changes,
    }
    if previous:
        command.update(blocker_id=previous["id"], expected_blocker_version=previous["version"])
    return await c.post("/api/v1/blockers", json=command, headers={"Idempotency-Key": str(uuid4())})


@pytest.mark.asyncio
async def test_blocker_lifecycle_idempotency_and_history(harness: Harness):
    task = await seed_task(harness)
    async with AsyncClient(
        transport=ASGITransport(app=blocker_app(harness)), base_url="http://test"
    ) as c:
        current = None
        for action in ("CREATE", "ACKNOWLEDGE", "RESOLVE", "REOPEN"):
            response = await apply(c, task, action, current)
            assert response.status_code == 200, response.text
            current = response.json()
        history = (await c.get(f"/api/v1/blockers/{current['id']}/history")).json()
        assert [event["to_status"] for event in history] == [
            "OPEN",
            "ACKNOWLEDGED",
            "RESOLVED",
            "OPEN",
        ]
        assert current["status"] == "OPEN"
        assert (await apply(c, task, "RESOLVE", {**current, "version": 1})).status_code == 409
        key = str(uuid4())
        command = {
            "task_id": task,
            "expected_task_version": 1,
            "action": "CREATE",
            "text": "Second blocker",
        }
        first = await c.post("/api/v1/blockers", json=command, headers={"Idempotency-Key": key})
        again = await c.post("/api/v1/blockers", json=command, headers={"Idempotency-Key": key})
        assert first.json() == again.json()
        assert len((await c.get(f"/api/v1/blockers?task_id={task}")).json()) == 2


@pytest.mark.asyncio
async def test_failed_blocker_rolls_back_report_confirmation(harness: Harness):
    task = await seed_task(harness)
    command = {
        "task_id": task,
        "expected_task_version": 1,
        "action": "RESOLVE",
        "blocker_id": str(uuid4()),
        "expected_blocker_version": 1,
    }
    async with AsyncClient(
        transport=ASGITransport(app=blocker_app(harness)), base_url="http://test"
    ) as c:
        created = await draft(c, [body(task, blocker_commands=[command])])
        response = await confirm(c, created)
        assert response.status_code in {404, 409}
        assert (await c.get(f"/api/v1/daily-updates?task_id={task}")).json() == []
        assert (await c.get(f"/api/v1/tasks/{task}/reporting-context")).json()[
            "progress_version"
        ] == 0


@pytest.mark.asyncio
async def test_confirmed_report_applies_blocker_once(harness: Harness):
    task = await seed_task(harness)
    command = {
        "task_id": task,
        "expected_task_version": 1,
        "action": "CREATE",
        "severity": "CRITICAL",
        "text": "Cannot proceed",
    }
    async with AsyncClient(
        transport=ASGITransport(app=blocker_app(harness)), base_url="http://test"
    ) as c:
        created = await draft(c, [body(task, blocker_commands=[command])])
        assert (await c.get(f"/api/v1/blockers?task_id={task}")).json() == []
        key = str(uuid4())
        first, again = await confirm(c, created, key), await confirm(c, created, key)
        assert first.status_code == 201 and first.json() == again.json()
        assert len((await c.get(f"/api/v1/blockers?task_id={task}")).json()) == 1


@pytest.mark.asyncio
async def test_current_assignment_manager_tenant_and_immutable_history(harness: Harness):
    from sqlalchemy.exc import DBAPIError

    from app.modules.identity.api.dependencies import get_authenticated_actor

    task = await seed_task(harness)
    app = blocker_app(harness)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        current = (await apply(c, task)).json()
        app.dependency_overrides[get_authenticated_actor] = lambda: harness.peer
        assert (await c.get(f"/api/v1/blockers?task_id={task}")).status_code == 200
        app.dependency_overrides[get_authenticated_actor] = lambda: harness.actor
        await harness.sql(
            "UPDATE memberships SET role='EMPLOYEE' WHERE id=:m", {"m": harness.peer.membership_id}
        )
        await harness.sql(
            "UPDATE tasks SET assignee_membership_id=:m,version=version+1 WHERE id=:t",
            {"m": harness.peer.membership_id, "t": UUID(task)},
        )
        assert (await c.get(f"/api/v1/blockers?task_id={task}")).status_code == 404
        assert (
            await apply(c, task, "RESOLVE", current, expected_task_version=2)
        ).status_code == 404
        app.dependency_overrides[get_authenticated_actor] = lambda: harness.peer
        assert (await c.get(f"/api/v1/blockers?task_id={task}")).status_code == 200
        resolved = await apply(c, task, "RESOLVE", current, expected_task_version=2)
        assert resolved.status_code == 200, resolved.text
        app.dependency_overrides[get_authenticated_actor] = lambda: harness.foreign
        assert (await c.get(f"/api/v1/blockers/{current['id']}/history")).status_code == 404
        assert (
            await apply(c, task, "RESOLVE", resolved.json(), expected_task_version=2)
        ).status_code == 404
    await harness.sql("SET LOCAL ROLE app_runtime")
    await harness.connection.execute(
        __import__("sqlalchemy").text(
            "SELECT "
            "set_config('app.organization_id',:org,true),set_config('app.membership_id',:m,true)"
        ),
        {"org": str(harness.foreign.organization_id), "m": str(harness.foreign.membership_id)},
    )
    for table in ("blockers", "blocker_transitions", "blocker_evidence_links"):
        assert (
            await harness.connection.scalar(
                __import__("sqlalchemy").text(
                    f"SELECT count(*) FROM {table} WHERE organization_id=:org"
                ),
                {"org": harness.actor.organization_id},
            )
            == 0
        )
    await harness.sql("RESET ROLE")
    with pytest.raises(DBAPIError):
        async with harness.connection.begin_nested():
            await harness.sql(
                "UPDATE blocker_transitions SET version=99 WHERE blocker_id=:id",
                {"id": UUID(current["id"])},
            )
    with pytest.raises(DBAPIError):
        async with harness.connection.begin_nested():
            await harness.sql(
                "DELETE FROM blocker_transitions WHERE blocker_id=:id", {"id": UUID(current["id"])}
            )


@pytest.mark.asyncio
async def test_blocker_evidence_is_retained_and_readable_only_to_permitted_scope(harness: Harness):
    from app.modules.identity.api.dependencies import get_authenticated_actor
    from tests.test_evidence_api_integration import upload

    task = await seed_task(harness)
    app = blocker_app(harness)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        proof = (await upload(c)).json()
        created = (
            await apply(
                c, task, evidence_refs=[{"evidence_id": proof["evidence_id"], "version": 1}]
            )
        ).json()
        archived = await apply(c, task, "ARCHIVE", created)
        assert archived.status_code == 200 and archived.json()["archived"]
        assert archived.json()["evidence_refs"] == created["evidence_refs"]
        app.dependency_overrides[get_authenticated_actor] = lambda: harness.peer
        link = f"/api/v1/evidence/{proof['evidence_id']}/versions/1/content"
        assert (await c.get(link)).status_code == 200
        app.dependency_overrides[get_authenticated_actor] = lambda: harness.foreign
        assert (await c.get(link)).status_code == 404


@pytest.mark.asyncio
async def test_replay_rechecks_current_assignment(harness: Harness):
    task = await seed_task(harness)
    key = str(uuid4())
    command = {
        "task_id": task,
        "expected_task_version": 1,
        "action": "CREATE",
        "text": "Private blocker",
    }
    async with AsyncClient(
        transport=ASGITransport(app=blocker_app(harness)), base_url="http://test"
    ) as c:
        assert (
            await c.post("/api/v1/blockers", json=command, headers={"Idempotency-Key": key})
        ).status_code == 200
        await harness.sql(
            "UPDATE tasks SET assignee_membership_id=:m WHERE id=:t",
            {"m": harness.peer.membership_id, "t": UUID(task)},
        )
        assert (
            await c.post("/api/v1/blockers", json=command, headers={"Idempotency-Key": key})
        ).status_code == 404
