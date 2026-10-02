"""Evidence assessments and non-bypassable warning confirmations.
Revision ID: 0024
Revises: 0023
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0024"
down_revision: str | Sequence[str] | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evidence_assessments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("draft_version", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("owner_membership_id", sa.Uuid(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("binding", JSONB(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "draft_id", "draft_version", "attempt"),
        sa.ForeignKeyConstraint(
            ["organization_id", "draft_id", "draft_version"],
            [
                "daily_update_draft_revisions.organization_id",
                "daily_update_draft_revisions.draft_id",
                "daily_update_draft_revisions.version",
            ],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "owner_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('PENDING','READY','UNAVAILABLE','NOT_ASSESSED_NO_EVIDENCE')", name="state"
        ),
        sa.CheckConstraint("attempt > 0", name="attempt_positive"),
    )
    op.create_index(
        "ix_evidence_assessments_draft",
        "evidence_assessments",
        ["organization_id", "draft_id", "draft_version", "attempt"],
    )
    op.create_table(
        "warning_acknowledgments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("update_id", sa.Uuid(), nullable=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=False),
        sa.Column("owner_membership_id", sa.Uuid(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "update_id"),
        sa.ForeignKeyConstraint(
            ["organization_id", "update_id"],
            ["daily_updates.organization_id", "daily_updates.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "assessment_id"],
            ["evidence_assessments.organization_id", "evidence_assessments.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "owner_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
    )
    tenant = "organization_id = nullif(current_setting('app.organization_id',true),'')::uuid"
    member = "nullif(current_setting('app.membership_id',true),'')::uuid"
    for table in ("evidence_assessments", "warning_acknowledgments"):
        scope = (
            f"{tenant} AND owner_membership_id={member} AND EXISTS ("
            f"SELECT 1 FROM memberships m WHERE m.organization_id={table}.organization_id "
            f"AND m.id={member} AND m.is_active AND EXISTS (SELECT 1 FROM users u "
            "WHERE u.id=m.user_id AND u.is_active))"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY assessment_scope ON {table} TO app_runtime "
            f"USING ({scope}) WITH CHECK ({scope})"
        )
        op.execute(f"GRANT SELECT, INSERT ON {table} TO app_runtime")
    op.execute("GRANT UPDATE ON evidence_assessments TO app_runtime")
    op.execute(
        "CREATE TRIGGER acknowledgment_immutable BEFORE UPDATE OR DELETE ON "
        "warning_acknowledgments FOR EACH ROW EXECUTE FUNCTION guard_daily_reporting_fact()"
    )
    op.execute("""CREATE FUNCTION guard_evidence_assessment() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable assessment'; END IF;
      IF OLD.state <> 'PENDING' OR NEW.state='PENDING' OR
         ROW(NEW.id,NEW.organization_id,NEW.draft_id,NEW.draft_version,NEW.attempt,
             NEW.owner_membership_id,NEW.content_hash,NEW.binding,NEW.created_at,NEW.deadline)
         IS DISTINCT FROM ROW(OLD.id,OLD.organization_id,OLD.draft_id,OLD.draft_version,
             OLD.attempt,OLD.owner_membership_id,OLD.content_hash,OLD.binding,
             OLD.created_at,OLD.deadline) THEN
         RAISE EXCEPTION 'immutable assessment';
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER assessment_immutable BEFORE UPDATE OR DELETE ON evidence_assessments "
        "FOR EACH ROW EXECUTE FUNCTION guard_evidence_assessment()"
    )


def downgrade() -> None:
    raise RuntimeError("Assessment and acknowledgment history requires a forward migration")
