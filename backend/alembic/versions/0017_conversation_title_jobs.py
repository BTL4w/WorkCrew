"""Allow one independent naming job per conversation.

Revision ID: 0017
Revises: 0016
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_assistant_jobs_job_type"), "assistant_jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_assistant_jobs_job_type"),
        "assistant_jobs",
        "job_type IN ('assistant.turn.execute', 'assistant.conversation.title')",
    )
    op.create_index(
        "uq_assistant_jobs_conversation_title",
        "assistant_jobs",
        ["organization_id", "conversation_id"],
        unique=True,
        postgresql_where=sa.text("job_type = 'assistant.conversation.title'"),
    )


def downgrade() -> None:
    # Fail safely if naming jobs exist; do not erase durable job/audit evidence.
    op.execute(
        sa.text("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM assistant_jobs
                       WHERE job_type = 'assistant.conversation.title') THEN
                RAISE EXCEPTION 'naming jobs exist; retain revision 0017';
            END IF;
        END $$;
    """)
    )
    op.drop_index("uq_assistant_jobs_conversation_title", table_name="assistant_jobs")
    op.drop_constraint(op.f("ck_assistant_jobs_job_type"), "assistant_jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_assistant_jobs_job_type"), "assistant_jobs", "job_type = 'assistant.turn.execute'"
    )
