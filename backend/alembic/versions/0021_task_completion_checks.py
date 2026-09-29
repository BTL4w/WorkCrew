"""Append immutable, tenant-scoped completion attestations.

Revision ID: 0021
Revises: 0020
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_task_status_transitions_organization_id_id",
        "task_status_transitions",
        ["organization_id", "id"],
    )
    op.create_table(
        "task_completion_checks",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("transition_id", sa.Uuid(), nullable=False),
        sa.Column("actor_membership_id", sa.Uuid(), nullable=False),
        sa.Column("task_version_after", sa.Integer(), nullable=False),
        sa.Column("criterion_id", sa.Uuid(), nullable=True),
        sa.Column("criterion_version", sa.Integer(), nullable=True),
        sa.Column("confirmed", sa.Boolean(), nullable=False),
        sa.Column("observation_id", sa.Uuid(), nullable=True),
        sa.Column("evidence_refs", JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "transition_id"],
            ["task_status_transitions.organization_id", "task_status_transitions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "actor_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "observation_id"],
            ["task_progress_observations.organization_id", "task_progress_observations.id"],
            name="fk_task_completion_checks_observation_tenant",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "transition_id", "criterion_id"),
    )
    op.create_index(
        "ix_task_completion_checks_task",
        "task_completion_checks",
        ["organization_id", "task_id", "occurred_at"],
    )
    op.execute("ALTER TABLE task_completion_checks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE task_completion_checks FORCE ROW LEVEL SECURITY")
    tenant = "organization_id = nullif(current_setting('app.organization_id', true), '')::uuid"
    member = "nullif(current_setting('app.membership_id', true), '')::uuid"
    active = (
        "EXISTS (SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id "
        f"WHERE m.organization_id=organization_id AND m.id={member} "
        "AND m.is_active AND u.is_active)"
    )
    manager = (
        "EXISTS (SELECT 1 FROM memberships m WHERE m.organization_id=organization_id "
        f"AND m.id={member} AND m.is_active AND m.role IN ('MANAGER','ADMIN'))"
    )
    op.execute(
        f"CREATE POLICY completion_read ON task_completion_checks FOR SELECT TO app_runtime "
        f"USING ({tenant} AND {active} AND (actor_membership_id={member} OR {manager}))"
    )
    op.execute(
        f"CREATE POLICY completion_insert ON task_completion_checks FOR INSERT TO app_runtime "
        f"WITH CHECK ({tenant} AND {active} AND actor_membership_id={member})"
    )
    op.execute("GRANT SELECT, INSERT ON task_completion_checks TO app_runtime")
    op.execute(
        "CREATE TRIGGER completion_check_immutable BEFORE UPDATE OR DELETE ON "
        "task_completion_checks FOR EACH ROW EXECUTE FUNCTION guard_daily_reporting_fact()"
    )


def downgrade() -> None:
    raise RuntimeError("Completion attestations are audit history; use a forward migration")
