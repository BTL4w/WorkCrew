"""Direct PostgreSQL isolation and schema tests for AI planning runs tables."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.core.database import create_database_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]

DELETABLE_TABLES = {
    "workflow_checkpoints",
    "workflow_jobs",
}

STATEFUL_UPDATE_TABLES = {"proposals"}

UPDATE_ONLY_TABLES = {
    "workflow_runs",
    "approvals",
    "outbox_events",
}

UPDATE_COLUMNS = {
    "workflow_runs": {"status", "error_message", "version", "updated_at"},
    "approvals": {
        "status",
        "decided_by_membership_id",
        "decision_reason",
        "decided_at",
        "version",
        "updated_at",
    },
    "outbox_events": {
        "status",
        "attempt_count",
        "max_attempts",
        "available_at",
        "published_at",
        "last_error",
        "last_error_code",
        "locked_by_worker_id",
        "lease_until",
    },
}

APPEND_ONLY_TABLES = {
    "proposal_versions",
    "workflow_events",
    "model_invocations",
    "context_references",
}

AI_TABLES = DELETABLE_TABLES | STATEFUL_UPDATE_TABLES | UPDATE_ONLY_TABLES | APPEND_ONLY_TABLES


@pytest.mark.asyncio
async def test_ai_planning_tables_force_rls_and_runtime_has_proper_grants() -> None:
    engine = create_database_engine(Settings(environment="test"))
    try:
        async with engine.connect() as connection:
            flags = await connection.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname IN ("
                    "'workflow_runs', 'workflow_checkpoints', 'proposals', 'proposal_versions', "
                    "'approvals', 'workflow_jobs', 'workflow_events', 'model_invocations', "
                    "'context_references', 'outbox_events'"
                    ")"
                )
            )
            assert {
                row.relname: (row.relrowsecurity, row.relforcerowsecurity) for row in flags
            } == {table: (True, True) for table in AI_TABLES}

            grants = await connection.execute(
                text(
                    "SELECT table_name, privilege_type "
                    "FROM information_schema.role_table_grants "
                    "WHERE grantee = 'app_runtime' "
                    "AND table_name IN ("
                    "'workflow_runs', 'workflow_checkpoints', 'proposals', 'proposal_versions', "
                    "'approvals', 'workflow_jobs', 'workflow_events', 'model_invocations', "
                    "'context_references', 'outbox_events'"
                    ")"
                )
            )
            by_table: dict[str, set[str]] = {}
            for row in grants:
                by_table.setdefault(row.table_name, set()).add(row.privilege_type)

            for table in DELETABLE_TABLES:
                assert by_table[table] == {"SELECT", "INSERT", "UPDATE", "DELETE"}, (
                    f"Deletable table {table} has unexpected grants: {by_table[table]}"
                )

            for table in STATEFUL_UPDATE_TABLES:
                assert by_table[table] == {"SELECT", "INSERT", "UPDATE"}, (
                    f"Stateful table {table} has unexpected grants: {by_table[table]}"
                )

            for table in UPDATE_ONLY_TABLES:
                assert by_table[table] == {"SELECT", "INSERT"}, (
                    f"Update-only table {table} has unexpected grants: {by_table[table]}"
                )

            for table in APPEND_ONLY_TABLES:
                assert by_table[table] == {"SELECT", "INSERT"}, (
                    f"Append-only table {table} has unexpected grants: {by_table[table]}"
                )

            update_columns = await connection.execute(
                text(
                    "SELECT table_name, column_name "
                    "FROM information_schema.role_column_grants "
                    "WHERE grantee = 'app_runtime' AND privilege_type = 'UPDATE' "
                    "AND table_name IN ('workflow_runs', 'approvals', 'outbox_events')"
                )
            )
            actual_update_columns: dict[str, set[str]] = {}
            for row in update_columns:
                actual_update_columns.setdefault(row.table_name, set()).add(row.column_name)
            assert actual_update_columns == UPDATE_COLUMNS

            constraints = {
                row.conname: row.definition
                for row in await connection.execute(
                    text(
                        "SELECT conname, pg_get_constraintdef(oid) AS definition "
                        "FROM pg_constraint WHERE conname IN "
                        "('ck_approvals_ck_approvals_status', "
                        "'ck_approvals_ck_approvals_version', "
                        "'ck_workflow_runs_ck_workflow_runs_verifier_version', "
                        "'ck_workflow_runs_status', "
                        "'ck_proposals_ck_proposals_status', "
                        "'ck_outbox_events_ck_outbox_events_status', "
                        "'ck_proposal_versions_ck_proposal_versions_creator_type', "
                        "'uq_outbox_events_organization_event')"
                    )
                )
            }
            assert "DRAFT_AVAILABLE" in constraints["ck_workflow_runs_status"]
            assert "ck_proposals_ck_proposals_status" in constraints
            assert "ck_outbox_events_ck_outbox_events_status" in constraints
            assert "ck_proposal_versions_ck_proposal_versions_creator_type" in constraints
            assert "uq_outbox_events_organization_event" in constraints

            indexes = {
                row.indexname: row.indexdef
                for row in await connection.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE indexname IN "
                        "('ix_workflow_jobs_queue', 'ix_workflow_jobs_lease', "
                        "'ix_outbox_events_queue')"
                    )
                )
            }
            assert "(organization_id, status" in indexes["ix_workflow_jobs_queue"]
            assert "(organization_id, locked_by_worker_id" in indexes["ix_workflow_jobs_lease"]
            expected_queue_idx = "(organization_id, status, available_at, lease_until, id)"
            assert expected_queue_idx in indexes["ix_outbox_events_queue"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_can_update_outbox_delivery_but_not_event_evidence() -> None:
    engine = create_database_engine(Settings(environment="test"))
    organization_id, outbox_id, event_id, aggregate_id = (uuid4() for _ in range(4))
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(
                text(
                    "INSERT INTO organizations (id, slug, name) "
                    "VALUES (:id, :slug, 'Outbox grants')"
                ),
                {"id": organization_id, "slug": f"outbox-grants-{organization_id.hex}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO outbox_events "
                    "(id, organization_id, event_id, event_type, aggregate_type, "
                    "aggregate_id, payload) VALUES "
                    "(:id, :org, :event, 'proposal.approved', 'proposal', :aggregate, :payload)"
                ),
                {
                    "id": outbox_id,
                    "org": organization_id,
                    "event": event_id,
                    "aggregate": aggregate_id,
                    "payload": '{"immutable": true}',
                },
            )
            await connection.execute(text("SET LOCAL ROLE app_runtime"))
            await connection.execute(
                text("SELECT set_config('app.organization_id', :value, true)"),
                {"value": str(organization_id)},
            )

            await connection.execute(
                text(
                    "UPDATE outbox_events SET status = 'DISPATCHED', published_at = now() "
                    "WHERE id = :id"
                ),
                {"id": outbox_id},
            )
            assert (
                await connection.scalar(
                    text("SELECT status FROM outbox_events WHERE id = :id"), {"id": outbox_id}
                )
                == "DISPATCHED"
            )

            with pytest.raises(DBAPIError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            "UPDATE outbox_events "
                            "SET event_type = 'tampered', payload = :payload WHERE id = :id"
                        ),
                        {"id": outbox_id, "payload": '{"immutable": false}'},
                    )

            await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_cannot_read_foreign_ai_planning_rows() -> None:
    engine = create_database_engine(Settings(environment="test"))
    org_a, org_b, user_b, member_b, project_b, run_b = (uuid4() for _ in range(6))
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(
                text(
                    "INSERT INTO organizations (id, slug, name) VALUES "
                    "(:a, :sa, 'A'), (:b, :sb, 'B')"
                ),
                {
                    "a": org_a,
                    "sa": f"ai-read-a-{org_a.hex}",
                    "b": org_b,
                    "sb": f"ai-read-b-{org_b.hex}",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, email_normalized, email_display, display_name, password_hash) "
                    "VALUES (:id, :email, :email, 'B', 'hash')"
                ),
                {"id": user_b, "email": f"{user_b.hex}@example.test"},
            )
            await connection.execute(
                text(
                    "INSERT INTO memberships (id, organization_id, user_id, role) "
                    "VALUES (:id, :org, :user, 'MANAGER')"
                ),
                {"id": member_b, "org": org_b, "user": user_b},
            )
            await connection.execute(
                text(
                    "INSERT INTO projects (id, organization_id, name, "
                    "created_by_membership_id, updated_by_membership_id) "
                    "VALUES (:id, :org, 'B', :member, :member)"
                ),
                {"id": project_b, "org": org_b, "member": member_b},
            )
            await connection.execute(
                text(
                    "INSERT INTO workflow_runs (id, organization_id, project_id, "
                    "requested_by_membership_id, status, workflow_name, "
                    "workflow_version, verifier_version, input_goal_text) "
                    "VALUES (:id, :org, :project, :member, 'QUEUED', "
                    "'planning', 'v1.0', 'planning-verifier-v1', 'Goal B')"
                ),
                {
                    "id": run_b,
                    "org": org_b,
                    "project": project_b,
                    "member": member_b,
                },
            )

            await connection.execute(text("SET LOCAL ROLE app_runtime"))
            for context in (None, org_a):
                if context is not None:
                    await connection.execute(
                        text("SELECT set_config('app.organization_id', :value, true)"),
                        {"value": str(context)},
                    )
                visible = await connection.scalar(
                    text("SELECT count(*) FROM workflow_runs WHERE id = :id"),
                    {"id": run_b},
                )
                assert visible == 0
            await transaction.rollback()
    finally:
        await engine.dispose()
