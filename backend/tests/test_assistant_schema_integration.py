"""Direct PostgreSQL RLS and least-privilege tests for Assistant tables."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.core.database import create_database_engine
from tests.test_assistant_schema import ASSISTANT_TABLES

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


@pytest.mark.asyncio
async def test_tool_evidence_can_be_finalized_with_narrow_runtime_grant() -> None:
    engine = create_database_engine(Settings(environment="test"))
    try:
        async with engine.connect() as connection:
            grants = await connection.execute(
                text(
                    "SELECT column_name FROM information_schema.column_privileges "
                    "WHERE table_name = 'tool_invocations' AND grantee = 'app_runtime' "
                    "AND privilege_type = 'UPDATE'"
                )
            )
            assert {row.column_name for row in grants} == {
                "typed_output",
                "context_references",
                "status",
                "safe_error_code",
                "completed_at",
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_tool_terminal_guard_accepts_evidence_but_keeps_input_immutable() -> None:
    engine = create_database_engine(Settings(environment="test"))
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(
                text(
                    "CREATE TEMP TABLE tool_invocations "
                    "(id integer, status text, typed_input jsonb, typed_output jsonb, "
                    "context_references jsonb, safe_error_code text, completed_at timestamptz)"
                )
            )
            await connection.execute(
                text(
                    "CREATE TRIGGER guard BEFORE UPDATE ON tool_invocations "
                    "FOR EACH ROW EXECUTE FUNCTION protect_assistant_invocation_terminal()"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO tool_invocations "
                    "(id, status, typed_input, context_references) "
                    "VALUES (1, 'RUNNING', '{\"project_id\":\"exact\"}'::jsonb, '[]'::jsonb)"
                )
            )
            await connection.execute(
                text(
                    "UPDATE tool_invocations SET status='SUCCEEDED', "
                    "typed_output='{}'::jsonb, "
                    'context_references=\'[{"source":"verified"}]\'::jsonb, '
                    "completed_at=now() WHERE id=1"
                )
            )
            with pytest.raises(DBAPIError):
                async with connection.begin_nested():
                    await connection.execute(
                        text("UPDATE tool_invocations SET typed_input='{}'::jsonb WHERE id=1")
                    )
            await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_all_assistant_tables_force_rls_and_have_no_delete_grant() -> None:
    engine = create_database_engine(Settings(environment="test"))
    try:
        async with engine.connect() as connection:
            flags = await connection.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = ANY(:tables)"
                ),
                {"tables": list(ASSISTANT_TABLES)},
            )
            assert {
                row.relname: (row.relrowsecurity, row.relforcerowsecurity) for row in flags
            } == {name: (True, True) for name in ASSISTANT_TABLES}
            grants = await connection.execute(
                text(
                    "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                    "WHERE grantee = 'app_runtime' AND table_name = ANY(:tables)"
                ),
                {"tables": list(ASSISTANT_TABLES)},
            )
            by_table: dict[str, set[str]] = {}
            for row in grants:
                by_table.setdefault(row.table_name, set()).add(row.privilege_type)
            assert set(by_table) == ASSISTANT_TABLES
            assert all("DELETE" not in values for values in by_table.values())
            for name in {
                "assistant_messages",
                "agent_handoffs",
                "agent_checkpoints",
                "agent_context_references",
                "agent_model_invocations",
                "assistant_events",
            }:
                assert by_table[name] == {"SELECT", "INSERT"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_append_only_message_cannot_be_updated_as_app_runtime() -> None:
    engine = create_database_engine(Settings(environment="test"))
    organization_id, user_id, membership_id, conversation_id, message_id = (
        uuid4() for _ in range(5)
    )
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(
                text("INSERT INTO organizations (id, slug, name) VALUES (:id, :slug, 'Append')"),
                {"id": organization_id, "slug": f"append-{organization_id.hex}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, email_normalized, email_display, display_name, password_hash) "
                    "VALUES (:id, :email, :email, 'Append', 'hash')"
                ),
                {"id": user_id, "email": f"{user_id.hex}@example.test"},
            )
            await connection.execute(
                text(
                    "INSERT INTO memberships (id, organization_id, user_id, role) "
                    "VALUES (:id, :org, :user, 'MANAGER')"
                ),
                {"id": membership_id, "org": organization_id, "user": user_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO assistant_conversations "
                    "(id, organization_id, owner_membership_id, locale) "
                    "VALUES (:id, :org, :member, 'en')"
                ),
                {"id": conversation_id, "org": organization_id, "member": membership_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO assistant_messages "
                    "(id, organization_id, conversation_id, sequence, role, content_blocks, "
                    "created_by_membership_id) VALUES "
                    "(:id, :org, :conversation, 1, 'USER', :blocks, :member)"
                ),
                {
                    "id": message_id,
                    "org": organization_id,
                    "conversation": conversation_id,
                    "blocks": '[{"kind":"text","text":"immutable"}]',
                    "member": membership_id,
                },
            )
            await connection.execute(text("SET LOCAL ROLE app_runtime"))
            await connection.execute(
                text("SELECT set_config('app.organization_id', :value, true)"),
                {"value": str(organization_id)},
            )
            await connection.execute(
                text("SELECT set_config('app.membership_id', :value, true)"),
                {"value": str(membership_id)},
            )
            with pytest.raises(DBAPIError):
                async with connection.begin_nested():
                    await connection.execute(
                        text("UPDATE assistant_messages SET role = 'SYSTEM' WHERE id = :id"),
                        {"id": message_id},
                    )
            await transaction.rollback()
    finally:
        await engine.dispose()
