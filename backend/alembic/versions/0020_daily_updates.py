"""Manual daily update drafts, immutable observations and effective work logs.
Revision ID: 0020
Revises: 0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "daily_update_drafts",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("owner_membership_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("version", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("confirmed_update_id", sa.Uuid(), nullable=True, primary_key=False),
        sa.CheckConstraint("version > 0", name=op.f("ck_daily_update_drafts_version_positive")),
        sa.ForeignKeyConstraint(
            ["organization_id", "owner_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("organization_id", "id"),
    )
    op.create_table(
        "daily_update_draft_revisions",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("version", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("owner_membership_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False, primary_key=False),
        sa.Column("payload", JSONB(), nullable=False, primary_key=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "draft_id"],
            ["daily_update_drafts.organization_id", "daily_update_drafts.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "owner_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("organization_id", "draft_id", "version"),
        sa.UniqueConstraint("organization_id", "id"),
    )
    op.create_table(
        "daily_updates",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("owner_membership_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("draft_version", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False, primary_key=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "draft_id"],
            ["daily_update_drafts.organization_id", "daily_update_drafts.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "owner_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("organization_id", "draft_id"),
        sa.UniqueConstraint("organization_id", "id"),
    )
    op.create_table(
        "task_progress_observations",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("update_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("task_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("owner_membership_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("progress_version", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("reporting_date", sa.Date(), nullable=False, primary_key=False),
        sa.Column("reporting_at", sa.DateTime(timezone=True), nullable=False, primary_key=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False, primary_key=False),
        sa.Column(
            "reported_percent", sa.Numeric(precision=12, scale=4), nullable=False, primary_key=False
        ),
        sa.Column(
            "remaining_hours", sa.Numeric(precision=12, scale=4), nullable=True, primary_key=False
        ),
        sa.Column("corrects_observation_id", sa.Uuid(), nullable=True, primary_key=False),
        sa.Column("payload", JSONB(), nullable=False, primary_key=False),
        sa.CheckConstraint(
            "remaining_hours BETWEEN 0 AND 10000",
            name=op.f("ck_task_progress_observations_remaining_hours_range"),
        ),
        sa.CheckConstraint(
            "reported_percent BETWEEN 0 AND 100",
            name=op.f("ck_task_progress_observations_reported_percent_range"),
        ),
        sa.CheckConstraint(
            "progress_version > 0", name=op.f("ck_task_progress_observations_version_positive")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "corrects_observation_id"],
            ["task_progress_observations.organization_id", "task_progress_observations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "owner_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "update_id"],
            ["daily_updates.organization_id", "daily_updates.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("organization_id", "corrects_observation_id"),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "task_id", "progress_version"),
    )
    op.create_index(
        "ix_task_progress_observations_task",
        "task_progress_observations",
        ["organization_id", "task_id", "reporting_at", "progress_version"],
    )
    op.create_table(
        "task_actual_projections",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("task_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("progress_version", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("observation_id", sa.Uuid(), nullable=True, primary_key=False),
        sa.CheckConstraint(
            "progress_version >= 0", name=op.f("ck_task_actual_projections_version_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "observation_id"],
            ["task_progress_observations.organization_id", "task_progress_observations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "task_id"),
    )
    op.create_table(
        "work_logs",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("observation_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("owner_membership_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("reporting_date", sa.Date(), nullable=False, primary_key=False),
        sa.Column(
            "spent_hours", sa.Numeric(precision=12, scale=4), nullable=False, primary_key=False
        ),
        sa.Column("supersedes_log_id", sa.Uuid(), nullable=True, primary_key=False),
        sa.CheckConstraint(
            "spent_hours BETWEEN 0 AND 24", name=op.f("ck_work_logs_spent_hours_range")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "observation_id"],
            ["task_progress_observations.organization_id", "task_progress_observations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "owner_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "supersedes_log_id"],
            ["work_logs.organization_id", "work_logs.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "observation_id"),
        sa.UniqueConstraint("organization_id", "supersedes_log_id"),
    )
    op.create_index(
        "ix_work_logs_owner_date",
        "work_logs",
        ["organization_id", "owner_membership_id", "reporting_date"],
    )
    op.create_table(
        "daily_update_evidence_links",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("observation_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("evidence_version", sa.Integer(), nullable=False, primary_key=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "evidence_id", "evidence_version"],
            [
                "evidence_originals.organization_id",
                "evidence_originals.id",
                "evidence_originals.version",
            ],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "observation_id"],
            ["task_progress_observations.organization_id", "task_progress_observations.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "observation_id", "evidence_id", "evidence_version"),
    )
    op.create_table(
        "weekly_actual_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("project_week_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("owner_membership_id", sa.Uuid(), nullable=False, primary_key=False),
        sa.Column("update_id", sa.Uuid(), nullable=True, primary_key=False),
        sa.Column("kind", sa.String(length=16), nullable=False, primary_key=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, primary_key=False),
        sa.Column("payload", JSONB(), nullable=False, primary_key=False),
        sa.CheckConstraint(
            "kind IN ('CURRENT', 'LATE', 'FINAL')", name=op.f("ck_weekly_actual_snapshots_kind")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "owner_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_week_id"],
            ["project_weeks.organization_id", "project_weeks.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "update_id"],
            ["daily_updates.organization_id", "daily_updates.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("organization_id", "id"),
    )
    op.create_index(
        "ix_weekly_actual_snapshots_week",
        "weekly_actual_snapshots",
        ["organization_id", "project_week_id", "captured_at"],
    )
    tenant = "organization_id = nullif(current_setting('app.organization_id', true), '')::uuid"
    member = "nullif(current_setting('app.membership_id', true), '')::uuid"
    active = (
        f"EXISTS (SELECT 1 FROM memberships m WHERE "
        f"m.organization_id=organization_id AND m.id={member} AND "
        f"m.is_active AND EXISTS (SELECT 1 FROM users u WHERE "
        f"u.id=m.user_id AND u.is_active))"
    )
    manager = (
        f"EXISTS (SELECT 1 FROM memberships m WHERE "
        f"m.organization_id=organization_id AND m.id={member} AND "
        f"m.is_active AND m.role IN ('MANAGER','ADMIN'))"
    )
    op.execute(
        "CREATE FUNCTION guard_daily_reporting_fact() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'immutable daily "
        "reporting fact'; END $$"
    )
    op.execute("ALTER TABLE daily_update_drafts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE daily_update_drafts FORCE ROW LEVEL SECURITY")
    read = f"{tenant} AND {active} AND ((owner_membership_id={member}))"
    write = f"{tenant} AND {active} AND (owner_membership_id={member})"
    op.execute(
        f"CREATE POLICY daily_read ON daily_update_drafts FOR SELECT TO app_runtime USING ({read})"
    )
    op.execute(
        f"CREATE POLICY daily_insert ON daily_update_drafts FOR INSERT TO "
        f"app_runtime WITH CHECK ({write})"
    )
    op.execute(
        f"CREATE POLICY daily_update ON daily_update_drafts FOR UPDATE TO "
        f"app_runtime USING ({write}) WITH CHECK ({write})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON daily_update_drafts TO app_runtime")
    op.execute("ALTER TABLE daily_update_draft_revisions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE daily_update_draft_revisions FORCE ROW LEVEL SECURITY")
    read = f"{tenant} AND {active} AND ((owner_membership_id={member}))"
    write = f"{tenant} AND {active} AND (owner_membership_id={member})"
    op.execute(
        f"CREATE POLICY daily_read ON daily_update_draft_revisions FOR "
        f"SELECT TO app_runtime USING ({read})"
    )
    op.execute(
        f"CREATE POLICY daily_insert ON daily_update_draft_revisions FOR "
        f"INSERT TO app_runtime WITH CHECK ({write})"
    )
    op.execute("GRANT SELECT, INSERT ON daily_update_draft_revisions TO app_runtime")
    op.execute(
        "CREATE TRIGGER daily_fact_immutable BEFORE UPDATE OR DELETE ON "
        "daily_update_draft_revisions FOR EACH ROW EXECUTE FUNCTION "
        "guard_daily_reporting_fact()"
    )
    op.execute("ALTER TABLE daily_updates ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE daily_updates FORCE ROW LEVEL SECURITY")
    read = f"{tenant} AND {active} AND ((owner_membership_id={member}) OR ({manager}))"
    write = f"{tenant} AND {active} AND (owner_membership_id={member})"
    op.execute(
        f"CREATE POLICY daily_read ON daily_updates FOR SELECT TO app_runtime USING ({read})"
    )
    op.execute(
        f"CREATE POLICY daily_insert ON daily_updates FOR INSERT TO "
        f"app_runtime WITH CHECK ({write})"
    )
    op.execute("GRANT SELECT, INSERT ON daily_updates TO app_runtime")
    op.execute(
        "CREATE TRIGGER daily_fact_immutable BEFORE UPDATE OR DELETE ON "
        "daily_updates FOR EACH ROW EXECUTE FUNCTION "
        "guard_daily_reporting_fact()"
    )
    op.execute("ALTER TABLE task_progress_observations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE task_progress_observations FORCE ROW LEVEL SECURITY")
    read = f"{tenant} AND {active} AND ((owner_membership_id={member}) OR ({manager}))"
    write = (
        f"{tenant} AND {active} AND (owner_membership_id={member} AND "
        f"EXISTS (SELECT 1 FROM tasks t WHERE "
        f"t.organization_id=task_progress_observations.organization_id AND "
        f"t.id=task_progress_observations.task_id AND "
        f"t.assignee_membership_id={member}))"
    )
    op.execute(
        f"CREATE POLICY daily_read ON task_progress_observations FOR "
        f"SELECT TO app_runtime USING ({read})"
    )
    op.execute(
        f"CREATE POLICY daily_insert ON task_progress_observations FOR "
        f"INSERT TO app_runtime WITH CHECK ({write})"
    )
    op.execute("GRANT SELECT, INSERT ON task_progress_observations TO app_runtime")
    op.execute(
        "CREATE TRIGGER daily_fact_immutable BEFORE UPDATE OR DELETE ON "
        "task_progress_observations FOR EACH ROW EXECUTE FUNCTION "
        "guard_daily_reporting_fact()"
    )
    op.execute("ALTER TABLE task_actual_projections ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE task_actual_projections FORCE ROW LEVEL SECURITY")
    read = (
        f"{tenant} AND {active} AND ((EXISTS (SELECT 1 FROM tasks t WHERE "
        f"t.organization_id=task_actual_projections.organization_id AND "
        f"t.id=task_actual_projections.task_id AND "
        f"t.assignee_membership_id={member})) OR ({manager}))"
    )
    write = (
        f"{tenant} AND {active} AND (EXISTS (SELECT 1 FROM tasks t WHERE "
        f"t.organization_id=task_actual_projections.organization_id AND "
        f"t.id=task_actual_projections.task_id AND "
        f"t.assignee_membership_id={member}))"
    )
    op.execute(
        f"CREATE POLICY daily_read ON task_actual_projections FOR SELECT "
        f"TO app_runtime USING ({read})"
    )
    op.execute(
        f"CREATE POLICY daily_insert ON task_actual_projections FOR INSERT "
        f"TO app_runtime WITH CHECK ({write})"
    )
    op.execute(
        f"CREATE POLICY daily_update ON task_actual_projections FOR UPDATE "
        f"TO app_runtime USING ({write}) WITH CHECK ({write})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON task_actual_projections TO app_runtime")
    op.execute("ALTER TABLE work_logs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE work_logs FORCE ROW LEVEL SECURITY")
    read = f"{tenant} AND {active} AND ((owner_membership_id={member}) OR ({manager}))"
    write = f"{tenant} AND {active} AND (owner_membership_id={member})"
    op.execute(f"CREATE POLICY daily_read ON work_logs FOR SELECT TO app_runtime USING ({read})")
    op.execute(
        f"CREATE POLICY daily_insert ON work_logs FOR INSERT TO app_runtime WITH CHECK ({write})"
    )
    op.execute("GRANT SELECT, INSERT ON work_logs TO app_runtime")
    op.execute(
        "CREATE TRIGGER daily_fact_immutable BEFORE UPDATE OR DELETE ON "
        "work_logs FOR EACH ROW EXECUTE FUNCTION "
        "guard_daily_reporting_fact()"
    )
    op.execute("ALTER TABLE daily_update_evidence_links ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE daily_update_evidence_links FORCE ROW LEVEL SECURITY")
    read = (
        f"{tenant} AND {active} AND ((EXISTS (SELECT 1 FROM "
        f"task_progress_observations o WHERE "
        f"o.organization_id=daily_update_evidence_links.organization_id "
        f"AND o.id=daily_update_evidence_links.observation_id AND "
        f"o.owner_membership_id={member})) OR ({manager}))"
    )
    write = (
        f"{tenant} AND {active} AND (EXISTS (SELECT 1 FROM "
        f"task_progress_observations o WHERE "
        f"o.organization_id=daily_update_evidence_links.organization_id "
        f"AND o.id=daily_update_evidence_links.observation_id AND "
        f"o.owner_membership_id={member}))"
    )
    op.execute(
        f"CREATE POLICY daily_read ON daily_update_evidence_links FOR "
        f"SELECT TO app_runtime USING ({read})"
    )
    op.execute(
        f"CREATE POLICY daily_insert ON daily_update_evidence_links FOR "
        f"INSERT TO app_runtime WITH CHECK ({write})"
    )
    op.execute("GRANT SELECT, INSERT ON daily_update_evidence_links TO app_runtime")
    op.execute(
        "CREATE TRIGGER daily_fact_immutable BEFORE UPDATE OR DELETE ON "
        "daily_update_evidence_links FOR EACH ROW EXECUTE FUNCTION "
        "guard_daily_reporting_fact()"
    )
    op.execute("ALTER TABLE weekly_actual_snapshots ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE weekly_actual_snapshots FORCE ROW LEVEL SECURITY")
    read = f"{tenant} AND {active} AND ((owner_membership_id={member}) OR ({manager}))"
    write = f"{tenant} AND {active} AND (owner_membership_id={member})"
    op.execute(
        f"CREATE POLICY daily_read ON weekly_actual_snapshots FOR SELECT "
        f"TO app_runtime USING ({read})"
    )
    op.execute(
        f"CREATE POLICY daily_insert ON weekly_actual_snapshots FOR INSERT "
        f"TO app_runtime WITH CHECK ({write})"
    )
    op.execute("GRANT SELECT, INSERT ON weekly_actual_snapshots TO app_runtime")
    op.execute(
        "CREATE TRIGGER daily_fact_immutable BEFORE UPDATE OR DELETE ON "
        "weekly_actual_snapshots FOR EACH ROW EXECUTE FUNCTION "
        "guard_daily_reporting_fact()"
    )

    # Narrow projection read spans historical assignees; private history/evidence stay under RLS.
    op.execute("""
CREATE FUNCTION public.read_task_reported_actual(requested_task uuid)
RETURNS TABLE (observation_id uuid, reported_percent numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT o.id, o.reported_percent
  FROM public.task_progress_observations o
  JOIN public.tasks t ON t.organization_id=o.organization_id AND t.id=o.task_id
  WHERE t.id=requested_task
    AND t.organization_id=nullif(current_setting('app.organization_id',true),'')::uuid
    AND EXISTS (
      SELECT 1 FROM public.memberships m JOIN public.users u ON u.id=m.user_id
      WHERE m.organization_id=t.organization_id
        AND m.id=nullif(current_setting('app.membership_id',true),'')::uuid
        AND m.is_active AND u.is_active
        AND (m.id=t.assignee_membership_id OR m.role IN ('MANAGER','ADMIN'))
    )
    AND NOT EXISTS (SELECT 1 FROM public.task_progress_observations corrected
      WHERE corrected.organization_id=o.organization_id
        AND corrected.corrects_observation_id=o.id)
  ORDER BY o.reporting_at DESC, o.progress_version DESC LIMIT 1
$$;
REVOKE ALL ON FUNCTION public.read_task_reported_actual(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.read_task_reported_actual(uuid) TO app_runtime;
    """)


def downgrade() -> None:
    raise RuntimeError("Reporting history must be retained; use a forward migration")
