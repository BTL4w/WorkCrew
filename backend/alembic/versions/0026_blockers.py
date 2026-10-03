"""Manual blocker lifecycle and immutable evidence/history.
Revision ID: 0026
Revises: 0025
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0026"
down_revision: str | Sequence[str] | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "blockers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_membership_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "id"),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("status IN ('OPEN','ACKNOWLEDGED','RESOLVED')", name="status"),
        sa.CheckConstraint("severity IN ('LOW','MEDIUM','HIGH','CRITICAL')", name="severity"),
        sa.CheckConstraint("version > 0", name="version"),
        sa.CheckConstraint("NOT archived OR status='RESOLVED'", name="archive_resolved"),
    )
    op.create_index("ix_blockers_task", "blockers", ["organization_id", "task_id", "status", "id"])
    op.create_table(
        "blocker_transitions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("blocker_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("actor_membership_id", sa.Uuid(), nullable=False),
        sa.Column("update_id", sa.Uuid(), nullable=True),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "blocker_id", "version"),
        sa.ForeignKeyConstraint(
            ["organization_id", "blocker_id"],
            ["blockers.organization_id", "blockers.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "actor_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "update_id"],
            ["daily_updates.organization_id", "daily_updates.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("version > 0", name="version"),
    )
    op.create_table(
        "blocker_evidence_links",
        sa.Column("organization_id", sa.Uuid(), primary_key=True),
        sa.Column("blocker_id", sa.Uuid(), primary_key=True),
        sa.Column("blocker_version", sa.Integer(), primary_key=True),
        sa.Column("evidence_id", sa.Uuid(), primary_key=True),
        sa.Column("evidence_version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "blocker_id", "blocker_version"],
            [
                "blocker_transitions.organization_id",
                "blocker_transitions.blocker_id",
                "blocker_transitions.version",
            ],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "evidence_id", "evidence_version"],
            [
                "evidence_originals.organization_id",
                "evidence_originals.id",
                "evidence_originals.version",
            ],
            ondelete="RESTRICT",
        ),
    )
    tenant = "organization_id=nullif(current_setting('app.organization_id',true),'')::uuid"
    member = "nullif(current_setting('app.membership_id',true),'')::uuid"
    active = (
        f"EXISTS (SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id "
        f"WHERE m.organization_id=blockers.organization_id AND m.id={member} AND "
        f"m.is_active AND u.is_active)"
    )
    task_scope = (
        f"EXISTS (SELECT 1 FROM tasks t JOIN memberships m ON "
        f"m.organization_id=t.organization_id AND m.id={member} WHERE "
        f"t.organization_id=blockers.organization_id AND t.id=blockers.task_id "
        f"AND (t.assignee_membership_id={member} OR m.role IN "
        f"('ADMIN','MANAGER')))"
    )
    op.execute("ALTER TABLE blockers ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE blockers FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY blocker_scope ON blockers TO app_runtime USING ({tenant} "
        f"AND {active} AND {task_scope}) WITH CHECK ({tenant} AND {active} AND "
        f"{task_scope})"
    )
    op.execute("GRANT SELECT,INSERT,UPDATE ON blockers TO app_runtime")
    for table in ("blocker_transitions", "blocker_evidence_links"):
        scope = (
            f"{tenant} AND EXISTS (SELECT 1 FROM blockers b WHERE "
            f"b.organization_id={table}.organization_id AND b.id={table}.blocker_id)"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY blocker_history_scope ON {table} TO app_runtime USING "
            f"({scope}) WITH CHECK ({scope})"
        )
        op.execute(f"GRANT SELECT,INSERT ON {table} TO app_runtime")
        op.execute(
            f"CREATE TRIGGER blocker_history_immutable BEFORE UPDATE OR DELETE ON "
            f"{table} FOR EACH ROW EXECUTE FUNCTION guard_daily_reporting_fact()"
        )
    op.execute("""CREATE FUNCTION guard_blocker_projection() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' OR OLD.archived OR NEW.version <> OLD.version+1 OR
        ROW(NEW.id,NEW.organization_id,NEW.task_id,NEW.created_by_membership_id,NEW.created_at)
              IS DISTINCT FROM
      ROW(OLD.id,OLD.organization_id,OLD.task_id,OLD.created_by_membership_id,OLD.created_at)
      THEN RAISE EXCEPTION 'immutable blocker identity or invalid version'; END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER blocker_projection_guard BEFORE UPDATE OR DELETE ON "
        "blockers FOR EACH ROW EXECUTE FUNCTION guard_blocker_projection()"
    )
    op.execute("""CREATE FUNCTION public.can_read_blocker_evidence(
    requested_id uuid, requested_version integer)
    RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=public,pg_temp AS $$
      SELECT EXISTS (
              SELECT 1 FROM blocker_evidence_links e JOIN blockers b ON
      b.organization_id=e.organization_id AND b.id=e.blocker_id
        JOIN tasks t ON t.organization_id=b.organization_id AND t.id=b.task_id
        JOIN memberships m ON m.organization_id=b.organization_id
        JOIN users u ON u.id=m.user_id
        WHERE e.organization_id=nullif(current_setting('app.organization_id',true),'')::uuid
          AND e.evidence_id=requested_id AND e.evidence_version=requested_version
                AND m.id=nullif(current_setting('app.membership_id',true),'')::uuid AND
      m.is_active AND u.is_active
          AND (m.role IN ('ADMIN','MANAGER') OR t.assignee_membership_id=m.id)
      )
    $$""")
    op.execute("REVOKE ALL ON FUNCTION public.can_read_blocker_evidence(uuid,integer) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.can_read_blocker_evidence(uuid,integer) TO app_runtime"
    )
    op.execute(
        "CREATE POLICY blocker_evidence_read ON evidence_originals FOR SELECT "
        "TO app_runtime USING (public.can_read_blocker_evidence(id,version))"
    )


def downgrade() -> None:
    raise RuntimeError("Blocker history requires a forward migration; never erase report evidence")
