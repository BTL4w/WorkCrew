"""Completion gates use real PostgreSQL RLS and append-only evidence."""

import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.modules.identity.api.dependencies import get_authenticated_actor
from tests.test_daily_update_api_integration import body, confirm, daily_app, draft, seed_task
from tests.test_evidence_api_integration import Harness, upload
from tests.test_evidence_api_integration import harness as harness

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


async def status(
    client: AsyncClient,
    task_id: str,
    target: str,
    version: int,
    attestations: list[dict[str, object]] | None = None,
    key: str | None = None,
):
    return await client.post(
        f"/api/v1/tasks/{task_id}/status",
        json={"to_status": target, "attestations": attestations or []},
        headers={"If-Match": f'"{version}"', "Idempotency-Key": key or str(uuid4())},
    )


@pytest.mark.asyncio
async def test_employee_done_requires_evidence_backed_report(harness: Harness) -> None:
    task_id = await seed_task(harness)
    app = daily_app(harness)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await status(client, task_id, "DONE", 1)
        assert missing.status_code == 409, missing.text
        assert missing.json()["error"]["code"] == "COMPLETION_REPORT_REQUIRED"
        evidence = await upload(client, key=str(uuid4()))
        assert evidence.status_code == 201, evidence.text
        report = await draft(
            client,
            [
                body(
                    task_id,
                    reported_percent="100",
                    evidence_refs=[
                        {
                            "evidence_id": evidence.json()["evidence_id"],
                            "version": evidence.json()["version"],
                        }
                    ],
                )
            ],
        )
        assert (await confirm(client, report)).status_code == 201
        key = str(uuid4())
        completed = await status(client, task_id, "DONE", 1, key=key)
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "DONE"
        assert completed.json()["version"] == 2
        replay = await status(client, task_id, "DONE", 1, key=key)
        assert replay.status_code == 200
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert (await status(client, task_id, "IN_PROGRESS", 2)).status_code == 200
        assert (await status(client, task_id, "DONE", 3)).json()["error"][
            "code"
        ] == "COMPLETION_REPORT_REQUIRED"
        fresh_report = await draft(
            client,
            [
                body(
                    task_id,
                    expected_task_version=3,
                    expected_progress_version=1,
                    reported_percent="100",
                    evidence_refs=[
                        {
                            "evidence_id": evidence.json()["evidence_id"],
                            "version": evidence.json()["version"],
                        }
                    ],
                )
            ],
        )
        assert (await confirm(client, fresh_report)).status_code == 201
        criterion_id = uuid4()
        await harness.sql(
            "INSERT INTO acceptance_criteria "
            "(id,organization_id,task_id,text,position,version,"
            "created_by_membership_id,updated_by_membership_id) "
            "VALUES (:id,:org,:task,'Check delivered result',1,1,:manager,:manager)",
            {
                "id": criterion_id,
                "org": harness.actor.organization_id,
                "task": task_id,
                "manager": harness.peer.membership_id,
            },
        )
        assert (await status(client, task_id, "DONE", 3)).json()["error"][
            "code"
        ] == "COMPLETION_CRITERIA_REQUIRED"
        assert (
            await status(
                client,
                task_id,
                "DONE",
                3,
                [{"criterion_id": str(criterion_id), "version": 1, "confirmed": True}],
            )
        ).status_code == 200
    count = await harness.sql(
        "SELECT count(*) FROM task_completion_checks WHERE task_id=:task", {"task": task_id}
    )
    assert count.scalar_one() == 2
    app.dependency_overrides[get_authenticated_actor] = lambda: harness.foreign
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await status(client, task_id, "DONE", 1)).status_code == 404
    await harness.sql("SET LOCAL ROLE app_runtime")
    await harness.connection.execute(
        text(
            "SELECT set_config('app.organization_id',:org,true), "
            "set_config('app.membership_id',:member,true)"
        ),
        {"org": str(harness.foreign.organization_id), "member": str(harness.foreign.membership_id)},
    )
    assert (
        await harness.connection.execute(text("SELECT count(*) FROM task_completion_checks"))
    ).scalar_one() == 0


@pytest.mark.asyncio
async def test_manager_completion_requires_current_criterion_and_no_report(
    harness: Harness,
) -> None:
    task_id = await seed_task(harness)
    criterion_id = uuid4()
    await harness.sql(
        "INSERT INTO acceptance_criteria "
        "(id,organization_id,task_id,text,position,version,"
        "created_by_membership_id,updated_by_membership_id) "
        "VALUES (:id,:org,:task,'Check output',1,2,:manager,:manager)",
        {
            "id": criterion_id,
            "org": harness.actor.organization_id,
            "task": task_id,
            "manager": harness.peer.membership_id,
        },
    )
    app = daily_app(harness)
    app.dependency_overrides[get_authenticated_actor] = lambda: harness.peer
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await status(client, task_id, "DONE", 1)
        assert missing.status_code == 409
        stale = await status(
            client,
            task_id,
            "DONE",
            1,
            [{"criterion_id": str(criterion_id), "version": 1, "confirmed": True}],
        )
        assert stale.status_code == 409
        completed = await status(
            client,
            task_id,
            "DONE",
            1,
            [{"criterion_id": str(criterion_id), "version": 2, "confirmed": True}],
        )
        assert completed.status_code == 200, completed.text
    rows = await harness.sql(
        "SELECT criterion_version,confirmed FROM task_completion_checks WHERE task_id=:task",
        {"task": task_id},
    )
    assert rows.one() == (2, True)
