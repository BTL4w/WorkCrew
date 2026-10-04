"""Metadata and migration contracts for tenant-owned Assistant persistence."""

from pathlib import Path

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

from app.core.database import Base
from app.modules.assistant.adapters import database_models as assistant_models

_MODEL_MODULES = (assistant_models,)
_MIGRATION = Path(__file__).parents[1] / "alembic/versions/0009_multi_agent_assistant.py"

ASSISTANT_TABLES = {
    "assistant_conversations",
    "assistant_messages",
    "assistant_turns",
    "orchestration_runs",
    "agent_runs",
    "agent_handoffs",
    "agent_checkpoints",
    "agent_context_references",
    "skill_invocations",
    "tool_invocations",
    "agent_model_invocations",
    "assistant_events",
    "assistant_jobs",
}


def _unique_columns(table_name: str) -> set[tuple[str, ...]]:
    return {
        tuple(constraint.columns.keys())
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, UniqueConstraint)
    }


def test_all_assistant_tables_are_tenant_owned_and_tenant_unique() -> None:
    assert Base.metadata.tables.keys() >= ASSISTANT_TABLES
    for table_name in ASSISTANT_TABLES:
        table = Base.metadata.tables[table_name]
        assert table.c.organization_id.nullable is False, table_name
        assert ("organization_id", "id") in _unique_columns(table_name), table_name


def test_every_uuid_relation_is_tenant_qualified() -> None:
    for table_name in ASSISTANT_TABLES:
        table = Base.metadata.tables[table_name]
        for constraint in table.constraints:
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            local_columns = tuple(constraint.column_keys)
            remote_columns = tuple(element.target_fullname for element in constraint.elements)
            assert local_columns[0] == "organization_id", (table_name, local_columns)
            assert remote_columns[0].endswith(".organization_id"), (
                table_name,
                remote_columns,
            )


def test_conversation_transcript_and_job_uniqueness_is_retry_safe() -> None:
    assert (
        "organization_id",
        "conversation_id",
        "sequence",
    ) in _unique_columns("assistant_messages")
    assert (
        "organization_id",
        "conversation_id",
        "dedupe_key",
    ) in _unique_columns("assistant_messages")
    assert ("organization_id", "user_message_id") in _unique_columns("assistant_turns")
    assert ("organization_id", "turn_id") in _unique_columns("orchestration_runs")
    assert (
        "organization_id",
        "conversation_id",
        "sequence",
    ) in _unique_columns("assistant_events")
    assert (
        "organization_id",
        "orchestration_run_id",
        "job_type",
    ) in _unique_columns("assistant_jobs")


def test_queue_and_owner_indexes_begin_with_tenant() -> None:
    conversations = Base.metadata.tables["assistant_conversations"]
    jobs = Base.metadata.tables["assistant_jobs"]
    conversation_indexes = {tuple(index.columns.keys()) for index in conversations.indexes}
    job_indexes = {tuple(index.columns.keys()) for index in jobs.indexes}

    assert ("organization_id", "owner_membership_id", "updated_at", "id") in conversation_indexes
    assert ("organization_id", "status", "available_at", "lease_until", "id") in job_indexes


def test_migration_forces_rls_and_declares_least_privilege_guards() -> None:
    migration = _MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "0009"' in migration
    assert 'down_revision: str | None = "0008"' in migration
    for table_name in ASSISTANT_TABLES:
        assert table_name in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "OWNER TO migration_owner" in migration
    assert "GRANT SELECT, INSERT" in migration
    assert "protect_assistant_invocation_terminal" in migration
    assert "REVOKE UPDATE, DELETE" in migration


def test_daily_summary_confirmation_block_is_public_contract():
    from typing import cast
    from uuid import uuid4

    from pydantic import TypeAdapter

    from app.modules.assistant.api.schemas import ContentBlock

    block = cast(
        ContentBlock,
        TypeAdapter(ContentBlock).validate_python(
            {
                "kind": "daily_summary",
                "project_id": str(uuid4()),
                "draft_id": str(uuid4()),
                "expected_version": 0,
                "operation": "CONFIGURE",
                "needs_manager_confirmation": True,
            }
        ),
    )
    assert block.kind == "daily_summary"
