"""Allow the runtime to finalize typed Tool evidence.

Revision ID: 0016
Revises: 0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("GRANT UPDATE (context_references) ON tool_invocations TO app_runtime"))
    op.execute(
        sa.text("""
        CREATE OR REPLACE FUNCTION protect_assistant_invocation_terminal()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            allowed_columns text[] := ARRAY[
                'typed_output', 'status', 'safe_error_code', 'completed_at'
            ];
        BEGIN
            IF OLD.status <> 'RUNNING' THEN
                RAISE EXCEPTION 'assistant invocation is already terminal';
            END IF;
            IF NEW.status = 'RUNNING' THEN
                RAISE EXCEPTION 'assistant invocation terminal update required';
            END IF;
            IF TG_TABLE_NAME = 'tool_invocations' THEN
                allowed_columns := array_append(allowed_columns, 'context_references');
            END IF;
            IF (to_jsonb(NEW) - allowed_columns)
               IS DISTINCT FROM (to_jsonb(OLD) - allowed_columns)
            THEN
                RAISE EXCEPTION 'assistant invocation identity and input are immutable';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    )


def downgrade() -> None:
    op.execute(
        sa.text("""
        CREATE OR REPLACE FUNCTION protect_assistant_invocation_terminal()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status <> 'RUNNING' THEN
                RAISE EXCEPTION 'assistant invocation is already terminal';
            END IF;
            IF NEW.status = 'RUNNING' THEN
                RAISE EXCEPTION 'assistant invocation terminal update required';
            END IF;
            IF (to_jsonb(NEW) - ARRAY['typed_output', 'status', 'safe_error_code', 'completed_at'])
               IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['typed_output', 'status', 'safe_error_code', 'completed_at'])
            THEN
                RAISE EXCEPTION 'assistant invocation identity and input are immutable';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    )
    op.execute(sa.text("REVOKE UPDATE (context_references) ON tool_invocations FROM app_runtime"))
