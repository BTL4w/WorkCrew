"""Immutable weekly entry baselines, plan history and role-aware RLS.

Revision ID: 0019
Revises: 0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "weekly_plan_baselines",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_week_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True)),
        sa.Column("payload", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_week_id"],
            ["project_weeks.organization_id", "project_weeks.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "project_week_id", "sequence"),
        sa.CheckConstraint("sequence > 0", name="sequence_positive"),
        sa.CheckConstraint("kind IN ('ENTRY', 'MANUAL', 'APPROVED')", name="kind"),
    )
    op.create_index(
        "ix_weekly_plan_baselines_week",
        "weekly_plan_baselines",
        ["organization_id", "project_week_id", "sequence"],
    )
    # Capture the facts available on activation; never claim older plans were reconstructed.
    op.execute("""
      INSERT INTO weekly_plan_baselines
        (id, organization_id, project_week_id, sequence, kind, captured_at, sealed_at, payload)
      SELECT gen_random_uuid(), w.organization_id, w.id, 1, 'ENTRY', now(),
        CASE WHEN w.status = 'COMPLETED' THEN now() END,
        jsonb_build_object('start_date', w.start_date, 'end_date', w.end_date,
          'week_version', w.version, 'status', w.status, 'objective', w.objective,
          'task_entries', coalesce((SELECT jsonb_agg(jsonb_build_object(
              'task_id', t.id, 'task_version', t.version, 'title', t.title,
              'captured_status', t.status,
              'effort_hours', t.estimated_effort_hours::text, 'due_date', t.due_date,
              'predecessor_ids', coalesce((SELECT
                jsonb_agg(d.predecessor_task_id ORDER BY d.predecessor_task_id)
                FROM task_dependencies d WHERE d.organization_id=t.organization_id
                  AND d.successor_task_id=t.id), '[]'::jsonb)
            ) ORDER BY t.id) FROM tasks t WHERE t.organization_id=w.organization_id
              AND t.project_week_id=w.id), '[]'::jsonb))
      FROM project_weeks w
    """)
    op.execute("ALTER TABLE weekly_plan_baselines ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE weekly_plan_baselines FORCE ROW LEVEL SECURITY")
    op.execute("""
      CREATE POLICY weekly_baseline_manager ON weekly_plan_baselines FOR ALL TO app_runtime
      USING (organization_id = nullif(current_setting('app.organization_id', true), '')::uuid
        AND EXISTS (SELECT 1 FROM memberships m
          WHERE m.organization_id=weekly_plan_baselines.organization_id
          AND m.id=nullif(current_setting('app.membership_id', true), '')::uuid
          AND m.is_active AND m.role IN ('MANAGER', 'ADMIN')))
      WITH CHECK (organization_id = nullif(current_setting('app.organization_id', true), '')::uuid
        AND EXISTS (SELECT 1 FROM memberships m
          WHERE m.organization_id=weekly_plan_baselines.organization_id
          AND m.id=nullif(current_setting('app.membership_id', true), '')::uuid
          AND m.is_active AND m.role IN ('MANAGER', 'ADMIN')))
    """)
    op.execute("GRANT SELECT, INSERT ON weekly_plan_baselines TO app_runtime")
    op.execute("""
      CREATE FUNCTION guard_weekly_baseline() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP = 'UPDATE' OR pg_trigger_depth() = 1
          OR jsonb_array_length(OLD.payload->'task_entries') > 0 THEN
          RAISE EXCEPTION 'immutable weekly plan baseline';
        END IF;
        RETURN OLD;
      END $$
    """)
    op.execute("""
      CREATE FUNCTION guard_weekly_baseline_reference() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.payload->'task_entries') e
          WHERE NOT EXISTS (SELECT 1 FROM tasks t
            WHERE t.organization_id=NEW.organization_id AND t.id=(e->>'task_id')::uuid))
          OR EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.payload->'task_entries') e,
            jsonb_array_elements_text(e->'predecessor_ids') p
            WHERE NOT EXISTS (SELECT 1 FROM tasks t
              WHERE t.organization_id=NEW.organization_id AND t.id=p::uuid)) THEN
          RAISE EXCEPTION 'invalid weekly plan task reference';
        END IF;
        RETURN NEW;
      END $$
    """)
    op.execute(
        "CREATE TRIGGER weekly_baseline_reference BEFORE INSERT ON weekly_plan_baselines "
        "FOR EACH ROW EXECUTE FUNCTION guard_weekly_baseline_reference()"
    )
    # Only snapshots with no historical task entries can cascade from a deletable empty week.
    op.execute(
        "CREATE TRIGGER weekly_baseline_immutable BEFORE UPDATE OR DELETE "
        "ON weekly_plan_baselines FOR EACH ROW EXECUTE FUNCTION guard_weekly_baseline()"
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM weekly_plan_baselines) THEN
      RAISE EXCEPTION 'plan history exists; retain revision 0019'; END IF; END $$""")
    op.drop_table("weekly_plan_baselines")
    op.execute("DROP FUNCTION guard_weekly_baseline()")
    op.execute("DROP FUNCTION guard_weekly_baseline_reference()")
