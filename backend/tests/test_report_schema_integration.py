"""Report tables enforce tenant ownership and append-only metric/source payloads."""

import os
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.test_report_api_integration import ReportHarness, report_harness

__all__ = ["report_harness"]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


@pytest.mark.asyncio
async def test_report_tables_forced_rls_and_runtime_cannot_mutate_snapshots(
    report_harness: ReportHarness,
):
    h = report_harness
    constraints = await h.sql(
        "SELECT conname FROM pg_constraint WHERE conrelid='reports'::regclass"
    )
    assert {"fk_reports_owned_snapshot", "fk_reports_owned_version"}.issubset(
        {r[0] for r in constraints}
    )
    flags = await h.sql(
        "SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE "
        "relname IN ('reports','report_metric_snapshots','report_versions',"
        "'report_snapshot_sources','report_snapshot_receipts')"
    )
    assert len(list(flags)) == 5
    flags = await h.sql(
        "SELECT count(*) FROM pg_class WHERE relname LIKE 'report_%' "
        "AND (NOT relrowsecurity OR NOT relforcerowsecurity)"
    )
    assert flags.scalar_one() == 0
    async with h.engine.begin() as connection:
        await connection.execute(text("SET LOCAL ROLE app_runtime"))
        assert await connection.scalar(text("SELECT count(*) FROM reports")) == 0
        grants = await connection.execute(
            text(
                "SELECT privilege_type FROM "
                "information_schema.role_table_grants WHERE grantee='app_runtime' "
                "AND table_name='report_metric_snapshots'"
            )
        )
        assert {r[0] for r in grants} == {"SELECT", "INSERT"}
        with pytest.raises(DBAPIError):
            await connection.execute(text("UPDATE report_metric_snapshots SET snapshot_hash='bad'"))


@pytest.mark.asyncio
async def test_cross_tenant_report_reference_rejected(report_harness: ReportHarness):
    h = report_harness
    async with h.engine.connect() as connection:
        transaction = await connection.begin()
        try:
            with pytest.raises(DBAPIError):
                await connection.execute(
                    text(
                        "INSERT INTO reports(id,organization_id,project_id,"
                        "kind,locale,version,snapshot_id,selected_version_id,created_by_membership_id,"
                        "narrative_requested,created_at) VALUES (:id,:org,:project,'DAILY','en',1,"
                        ":snapshot,:version,:member,false,now())"
                    ),
                    {
                        "id": uuid4(),
                        "org": h.foreign.organization_id,
                        "project": h.project_id,
                        "snapshot": uuid4(),
                        "version": uuid4(),
                        "member": h.foreign.membership_id,
                    },
                )
                await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_all_report_facts_are_tenant_bound_and_immutable(report_harness: ReportHarness):
    h = report_harness
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        created: list[dict[str, Any]] = []
        for _ in range(2):
            response = await client.post(
                "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
            )
            assert response.status_code == 201
            created.append(response.json())
    tables = (
        "reports",
        "report_metric_snapshots",
        "report_versions",
        "report_snapshot_sources",
        "report_snapshot_receipts",
    )
    for actor in (h.employee, h.foreign):
        async with h.engine.begin() as conn:
            await conn.execute(text("SET LOCAL ROLE app_runtime"))
            await conn.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true), "
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            for table in tables:
                assert await conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    for table in tables[1:]:
        async with h.engine.connect() as conn:
            txn = await conn.begin()
            try:
                with pytest.raises(DBAPIError):
                    await conn.execute(
                        text(f"DELETE FROM {table} WHERE organization_id=:org"),
                        {"org": h.actor.organization_id},
                    )
            finally:
                await txn.rollback()
    for pointer, foreign_id in (
        ("snapshot_id", created[1]["snapshot"]["id"]),
        ("selected_version_id", created[1]["selected_version"]["id"]),
    ):
        async with h.engine.connect() as conn:
            txn = await conn.begin()
            try:
                with pytest.raises(DBAPIError):
                    await conn.execute(
                        text(f"UPDATE reports SET {pointer}=:pointer WHERE id=:id"),
                        {"id": created[0]["report"]["id"], "pointer": foreign_id},
                    )
                    await conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            finally:
                await txn.rollback()
