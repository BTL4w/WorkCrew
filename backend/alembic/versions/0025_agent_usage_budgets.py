"""Durable Daily Update run and cumulative original-version usage.
Revision ID: 0025
Revises: 0024
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | Sequence[str] | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table, attempts, inputs, outputs in (
        ("agent_usage_budgets", 3, 24000, 4000),
        ("evidence_model_budgets", 10, 240000, 40000),
    ):
        constraints: list[sa.SchemaItem] = [
            sa.ForeignKeyConstraint(
                ["organization_id", "owner_membership_id"],
                ["memberships.organization_id", "memberships.id"],
                ondelete="RESTRICT",
            ),
            sa.CheckConstraint(f"attempts BETWEEN 0 AND {attempts}", name="attempts"),
            sa.CheckConstraint(f"input_tokens BETWEEN 0 AND {inputs}", name="inputs"),
            sa.CheckConstraint(f"output_tokens BETWEEN 0 AND {outputs}", name="outputs"),
            sa.CheckConstraint(
                "version = 0" if table == "agent_usage_budgets" else "version > 0", name="version"
            ),
        ]
        if table == "evidence_model_budgets":
            constraints.append(
                sa.ForeignKeyConstraint(
                    ["organization_id", "resource_id", "version"],
                    [
                        "evidence_originals.organization_id",
                        "evidence_originals.id",
                        "evidence_originals.version",
                    ],
                    ondelete="RESTRICT",
                )
            )
        op.create_table(
            table,
            sa.Column("organization_id", sa.Uuid(), primary_key=True),
            sa.Column("owner_membership_id", sa.Uuid(), primary_key=True),
            sa.Column("resource_id", sa.Uuid(), primary_key=True),
            sa.Column("version", sa.Integer(), primary_key=True),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("input_tokens", sa.Integer(), nullable=False),
            sa.Column("output_tokens", sa.Integer(), nullable=False),
            sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
            *constraints,
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        scope = (
            "organization_id=nullif(current_setting('app.organization_id',true),'')::uuid "
            "AND owner_membership_id=nullif(current_setting('app.membership_id',true),'')::uuid "
            "AND EXISTS(SELECT 1 FROM memberships m WHERE "
            f"m.organization_id={table}.organization_id "
            f"AND m.id={table}.owner_membership_id AND m.is_active "
            "AND EXISTS(SELECT 1 FROM users u WHERE u.id=m.user_id AND u.is_active))"
        )
        op.execute(
            f"CREATE POLICY daily_usage_scope ON {table} TO app_runtime "
            f"USING ({scope}) WITH CHECK ({scope})"
        )
        op.execute(f"GRANT SELECT,INSERT,UPDATE ON {table} TO app_runtime")
    op.execute("""CREATE FUNCTION guard_daily_model_usage() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'usage is durable'; END IF;
      IF ROW(NEW.organization_id,NEW.owner_membership_id,NEW.resource_id,NEW.version,NEW.deadline)
         IS DISTINCT FROM ROW(OLD.organization_id,OLD.owner_membership_id,
                              OLD.resource_id,OLD.version,OLD.deadline)
         OR NEW.attempts < OLD.attempts OR NEW.input_tokens < OLD.input_tokens
         OR NEW.output_tokens < OLD.output_tokens THEN RAISE EXCEPTION 'usage cannot reset'; END IF;
      RETURN NEW;
    END $$""")
    for table in ("agent_usage_budgets", "evidence_model_budgets"):
        op.execute(
            f"CREATE TRIGGER usage_monotonic BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION guard_daily_model_usage()"
        )


def downgrade() -> None:
    raise RuntimeError("Usage counters must not be reset by downgrade")
