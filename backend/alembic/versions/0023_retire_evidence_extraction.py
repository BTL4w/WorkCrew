"""Retire extraction access while preserving original and audit history.

Revision ID: 0023
Revises: 0022
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No deletion of originals, extracted history, model usage or audit evidence.
    # Old binaries cannot enqueue, process or expose retired operational rows.
    op.execute("REVOKE ALL ON evidence_processing_jobs, evidence_segments FROM app_runtime")


def downgrade() -> None:
    # Restore only prior privileges; RLS and immutable-history triggers stay enabled.
    op.execute("GRANT SELECT, INSERT, UPDATE ON evidence_processing_jobs TO app_runtime")
    op.execute("GRANT SELECT, INSERT ON evidence_segments TO app_runtime")
