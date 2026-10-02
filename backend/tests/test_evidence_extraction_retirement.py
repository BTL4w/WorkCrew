"""Removal keeps original uploads usable and closes former extraction routes."""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from tests.test_evidence_api_integration import Harness, harness, postgres, upload

__all__ = ["harness"]


@pytest.mark.integration
@postgres
@pytest.mark.asyncio
async def test_upload_keeps_original_without_queuing_extraction(harness: Harness) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=harness.app()), base_url="http://test"
    ) as client:
        created = await upload(client)
        assert created.status_code == 201
        original = created.json()
        assert (await upload(client)).json() == original
        root = f"/api/v1/evidence/{original['evidence_id']}/versions/1"
        assert (await client.get(root)).status_code == 200
        assert (await client.get(root + "/content")).status_code == 200
        assert (await client.get(root + "/extraction")).status_code == 404
        assert (
            await client.post(
                root + "/extraction", headers={"Idempotency-Key": "retired-extract-01"}
            )
        ).status_code == 404
        assert (await client.get(root + f"/preview/{uuid4()}")).status_code == 404
    count = await harness.sql(
        "SELECT count(*) FROM evidence_processing_jobs WHERE organization_id=:org",
        {"org": harness.actor.organization_id},
    )
    assert count.scalar_one() == 0


@pytest.mark.integration
@postgres
@pytest.mark.asyncio
async def test_retired_extraction_tables_are_inaccessible_to_runtime(harness: Harness) -> None:
    for table in ("evidence_processing_jobs", "evidence_segments"):
        privileges = await harness.sql(
            "SELECT has_table_privilege('app_runtime', :table, 'SELECT'), "
            "has_table_privilege('app_runtime', :table, 'INSERT'), "
            "has_table_privilege('app_runtime', :table, 'UPDATE')",
            {"table": table},
        )
        assert tuple(privileges.one()) == (False, False, False)
