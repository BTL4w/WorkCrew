"""Transaction-bound tenant revisions for optional workload cache reads.

Revision ID: f9128a14d001
Revises: a1b7c9d3e501
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9128a14d001"
down_revision: str | Sequence[str] | None = "a1b7c9d3e501"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCES = ("project_weeks", "memberships", "capacity_entries", "leave_entries", "tasks")


def upgrade() -> None:
    op.create_table(
        "read_model_generations",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("epoch", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("organization_id"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.CheckConstraint("generation >= 0", name="nonnegative_generation"),
    )
    op.execute("ALTER TABLE read_model_generations OWNER TO migration_owner")
    op.execute("ALTER TABLE read_model_generations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE read_model_generations FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON read_model_generations "
        "USING (organization_id=NULLIF(current_setting('app.organization_id',true),'')::uuid) "
        "WITH CHECK (organization_id=NULLIF(current_setting('app.organization_id',true),'')::uuid)"
    )
    op.execute("GRANT SELECT,INSERT,UPDATE ON read_model_generations TO app_runtime")
    op.execute("""
        CREATE FUNCTION public.bump_workload_generation() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
        DECLARE org uuid; old_org uuid; new_org uuid;
        BEGIN
            IF TG_OP <> 'INSERT' THEN old_org := OLD.organization_id; END IF;
            IF TG_OP <> 'DELETE' THEN new_org := NEW.organization_id; END IF;
            FOR org IN SELECT DISTINCT value FROM unnest(ARRAY[old_org,new_org]) value
                       WHERE value IS NOT NULL ORDER BY value
            LOOP
                INSERT INTO public.read_model_generations (organization_id,generation)
                VALUES (org,1) ON CONFLICT (organization_id) DO UPDATE
                SET generation=public.read_model_generations.generation+1,updated_at=now();
            END LOOP;
            RETURN NULL;
        END $$
    """)
    for table in _SOURCES:
        op.execute(
            f"CREATE TRIGGER workload_generation AFTER INSERT OR UPDATE OR DELETE "
            f"ON {table} FOR EACH ROW EXECUTE FUNCTION public.bump_workload_generation()"
        )
    op.execute(
        "INSERT INTO read_model_generations (organization_id) SELECT id FROM organizations "
        "ON CONFLICT (organization_id) DO NOTHING"
    )


def downgrade() -> None:
    # Normal rollback disables flags and keeps this additive schema. Destructive
    # downgrade is only for an explicitly authorized offline schema removal.
    for table in _SOURCES:
        op.execute(f"DROP TRIGGER workload_generation ON {table}")
    op.execute("DROP FUNCTION public.bump_workload_generation()")
    op.drop_table("read_model_generations")
