"""Versioned optional narrative settings and exact committed-summary report provenance."""

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "automation_schedule_versions",
        sa.Column("narrative_mode", sa.String(32), nullable=False, server_default="NONE"),
    )
    op.add_column(
        "automation_schedule_versions", sa.Column("creator_membership_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_schedule_version_creator",
        "automation_schedule_versions",
        "memberships",
        ["organization_id", "creator_membership_id"],
        ["organization_id", "id"],
    )
    op.create_check_constraint(
        op.f("ck_automation_schedule_versions_narrative_mode"),
        "automation_schedule_versions",
        "narrative_mode IN ('NONE','DRAFT_FOR_MANAGER')",
    )
    op.create_check_constraint(
        op.f("ck_automation_schedule_versions_narrative_creator"),
        "automation_schedule_versions",
        "narrative_mode='NONE' OR creator_membership_id IS NOT NULL",
    )
    op.add_column(
        "reports", sa.Column("origin", sa.String(32), nullable=False, server_default="ON_DEMAND")
    )
    op.add_column("reports", sa.Column("summary_id", sa.Uuid(), nullable=True))
    op.add_column("reports", sa.Column("summary_hash", sa.String(64), nullable=True))
    op.add_column("reports", sa.Column("workflow_version", sa.String(64), nullable=True))
    op.create_foreign_key(
        "fk_reports_summary",
        "reports",
        "daily_summary_snapshots",
        ["organization_id", "summary_id"],
        ["organization_id", "id"],
    )
    op.create_unique_constraint(
        "uq_reports_summary_workflow",
        "reports",
        ["organization_id", "summary_id", "locale", "workflow_version"],
    )
    op.create_check_constraint(
        op.f("ck_reports_origin"), "reports", "origin IN ('ON_DEMAND','DAILY_SUMMARY')"
    )
    op.create_check_constraint(
        op.f("ck_reports_summary_provenance"),
        "reports",
        "(origin='ON_DEMAND' AND summary_id IS NULL AND summary_hash IS NULL "
        "AND workflow_version IS NULL) OR (origin='DAILY_SUMMARY' AND summary_id IS NOT NULL "
        "AND summary_hash IS NOT NULL AND summary_hash ~ '^[a-f0-9]{64}$' "
        "AND workflow_version IS NOT NULL)",
    )


def downgrade():
    raise RuntimeError(
        "Forward-only provenance migration; restore from a documented backup if necessary"
    )
