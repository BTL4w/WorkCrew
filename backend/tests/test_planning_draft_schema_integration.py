"""Actual PostgreSQL protections for operational drafts and immutable versions."""

import os

import pytest
from sqlalchemy import text

from app.core.config import Settings
from app.core.database import create_database_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="PostgreSQL integration is opt-in"
    ),
]


@pytest.mark.asyncio
async def test_draft_tables_force_rls_and_versions_are_append_only() -> None:
    engine = create_database_engine(Settings(environment="test"))
    try:
        async with engine.connect() as connection:
            flags = await connection.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE "
                    "relname IN ('planning_drafts','planning_draft_versions')"
                )
            )
            assert {
                row.relname: (row.relrowsecurity, row.relforcerowsecurity) for row in flags
            } == {"planning_drafts": (True, True), "planning_draft_versions": (True, True)}
            grants = await connection.execute(
                text(
                    "SELECT privilege_type FROM information_schema.role_table_grants WHERE "
                    "grantee='app_runtime' AND table_name='planning_draft_versions'"
                )
            )
            assert {row.privilege_type for row in grants} == {"SELECT", "INSERT"}
            bypass = await connection.scalar(
                text("SELECT rolbypassrls FROM pg_roles WHERE rolname='app_runtime'")
            )
            assert bypass is False
    finally:
        await engine.dispose()
