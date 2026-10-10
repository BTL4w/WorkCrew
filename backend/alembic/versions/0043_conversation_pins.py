"""Persist owner chat pins; existing conversations remain unpinned.

Rollout: apply this additive migration before deploying the updated API.
Archive uses the existing status field and retains audit/execution records.
"""

import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistant_conversations",
        sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute("GRANT UPDATE (is_pinned) ON assistant_conversations TO app_runtime")


def downgrade() -> None:
    op.drop_column("assistant_conversations", "is_pinned")
