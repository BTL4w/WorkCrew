"""Snapshot sources paginate with binding, current access and captured versions."""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from tests.test_report_api_integration import ReportHarness, pytestmark, report_harness

__all__ = ["pytestmark", "report_harness"]


@pytest.mark.asyncio
async def test_report_sources_keep_capture_and_recheck_permission(report_harness: ReportHarness):
    h = report_harness
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        made = await client.post(
            "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
        )
        rid = made.json()["report"]["id"]
        await h.sql(
            "UPDATE tasks SET title='Updated survey',version=version+1 WHERE id=:id",
            {"id": h.task_id},
        )
        response = await client.get(f"/api/v1/reports/{rid}/sources?page_size=1")
        assert response.status_code == 200, response.text
        item = response.json()["items"][0]
        assert item["source"]["version"] == 1
        assert item["freshness"] == "UPDATED"
        assert item["source"]["resource_id"] == str(h.task_id)
        assert response.json()["next_cursor"] is None
        assert response.json()["total"] == 1
        assert (await client.get(f"/api/v1/reports/{rid}/sources?cursor=bad")).status_code == 422
    for actor in (h.employee, h.foreign):
        async with AsyncClient(
            transport=ASGITransport(app=h.app(actor)), base_url="http://test"
        ) as client:
            assert (await client.get(f"/api/v1/reports/{rid}/sources")).status_code == (
                403 if actor == h.employee else 404
            )


@pytest.mark.asyncio
async def test_source_pagination_full_inventory_and_cursor_report_binding(
    report_harness: ReportHarness,
):
    h = report_harness
    for index in range(124):
        await h.sql(
            "INSERT INTO "
            "tasks(id,organization_id,project_id,title,status,required_skill_labels,"
            "created_by_membership_id,updated_by_membership_id) "
            "VALUES (:id,:org,:project,:title,'TO_DO','[]',:member,:member)",
            {
                "id": uuid4(),
                "org": h.actor.organization_id,
                "project": h.project_id,
                "title": f"Task {index}",
                "member": h.actor.membership_id,
            },
        )
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        made = await client.post(
            "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
        )
        other = await client.post(
            "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
        )
        rid, other_id = made.json()["report"]["id"], other.json()["report"]["id"]
        first = (await client.get(f"/api/v1/reports/{rid}/sources?page_size=100")).json()
        assert first["total"] == 125 and len(first["items"]) == 100
        cursor = first["next_cursor"]
        second = (
            await client.get(
                f"/api/v1/reports/{rid}/sources", params={"page_size": 100, "cursor": cursor}
            )
        ).json()
        assert len(second["items"]) == 25 and second["next_cursor"] is None
        ids = [x["source"]["resource_id"] for x in first["items"] + second["items"]]
        assert len(set(ids)) == 125
        assert (
            await client.get(f"/api/v1/reports/{other_id}/sources", params={"cursor": cursor})
        ).status_code == 422
        assert (await client.get(f"/api/v1/reports/{rid}/sources?page_size=101")).status_code == 422
