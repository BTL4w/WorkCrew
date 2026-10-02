"""Durable evidence processing, tenant RLS, append-only extraction provenance.

Revision ID: 0022
Revises: 0021
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evidence_processing_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("uploader_membership_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("lease_id", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_attempts", sa.Integer(), nullable=False),
        sa.Column("model_attempts", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("next_visual", sa.Integer(), nullable=False),
        sa.Column("result", JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "evidence_id", "version"],
            [
                "evidence_originals.organization_id",
                "evidence_originals.id",
                "evidence_originals.version",
            ],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "uploader_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "evidence_id", "version", "revision"),
        sa.UniqueConstraint("organization_id", "uploader_membership_id", "idempotency_key"),
        sa.CheckConstraint(
            "state IN ('PENDING','PROCESSING','READY','PARTIAL','UNREADABLE','FAILED')",
            name="state",
        ),
        sa.CheckConstraint(
            "model_attempts BETWEEN 0 AND 10 AND lease_attempts BETWEEN 0 AND 3", name="budgets"
        ),
    )
    op.create_index(
        "ix_evidence_processing_jobs_ready",
        "evidence_processing_jobs",
        ["organization_id", "state", "lease_until"],
    )
    op.create_table(
        "evidence_segments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("content", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["evidence_processing_jobs.organization_id", "evidence_processing_jobs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "evidence_id", "version"],
            [
                "evidence_originals.organization_id",
                "evidence_originals.id",
                "evidence_originals.version",
            ],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "job_id", "source_id"),
    )
    op.create_index(
        "ix_evidence_segments_source",
        "evidence_segments",
        ["organization_id", "evidence_id", "version"],
    )
    tenant = "organization_id = nullif(current_setting('app.organization_id', true), '')::uuid"
    member = "nullif(current_setting('app.membership_id', true), '')::uuid"
    worker = "current_setting('app.evidence_job', true) = 'extract'"
    for table in ("evidence_processing_jobs", "evidence_segments"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY evidence_job_scope ON evidence_processing_jobs TO app_runtime "
        f"USING ({tenant} AND (uploader_membership_id={member} OR {worker})) "
        f"WITH CHECK ({tenant} AND (uploader_membership_id={member} OR {worker}))"
    )
    op.execute(
        f"""CREATE POLICY evidence_segment_scope ON evidence_segments TO app_runtime
        USING ({tenant} AND EXISTS (SELECT 1 FROM evidence_processing_jobs j
          WHERE j.organization_id=evidence_segments.organization_id
          AND j.id=evidence_segments.job_id))
        WITH CHECK ({tenant} AND EXISTS (SELECT 1 FROM evidence_processing_jobs j
          WHERE j.organization_id=evidence_segments.organization_id
          AND j.id=evidence_segments.job_id))"""
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON evidence_processing_jobs TO app_runtime")
    op.execute("GRANT SELECT, INSERT ON evidence_segments TO app_runtime")
    op.execute(
        "CREATE TRIGGER evidence_segment_immutable BEFORE UPDATE OR DELETE ON "
        "evidence_segments FOR EACH ROW EXECUTE FUNCTION guard_daily_reporting_fact()"
    )
    op.execute("""CREATE FUNCTION guard_evidence_processing_job() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.state IN ('READY','PARTIAL','UNREADABLE','FAILED') OR
         ROW(NEW.id,NEW.organization_id,NEW.evidence_id,NEW.version,
             NEW.uploader_membership_id,NEW.revision,NEW.idempotency_key,NEW.created_at)
         IS DISTINCT FROM ROW(OLD.id,OLD.organization_id,OLD.evidence_id,OLD.version,
             OLD.uploader_membership_id,OLD.revision,OLD.idempotency_key,OLD.created_at)
         OR NEW.model_attempts < OLD.model_attempts OR NEW.lease_attempts < OLD.lease_attempts
         OR NEW.next_visual < OLD.next_visual
         OR NEW.input_tokens < OLD.input_tokens OR NEW.output_tokens < OLD.output_tokens THEN
         RAISE EXCEPTION 'immutable extraction history or decreasing budget';
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER evidence_job_immutable BEFORE UPDATE ON evidence_processing_jobs "
        "FOR EACH ROW EXECUTE FUNCTION guard_evidence_processing_job()"
    )
    # Existing ready originals are queued without modifying their retained bytes.
    op.execute("""INSERT INTO evidence_processing_jobs
        (id,organization_id,evidence_id,version,uploader_membership_id,revision,
         idempotency_key,state,lease_until,lease_attempts,model_attempts,
         input_tokens,output_tokens,next_visual,created_at)
        SELECT gen_random_uuid(),organization_id,id,version,uploader_membership_id,1,
        'initial:' || id::text,'PENDING',now(),0,0,0,0,0,now() FROM evidence_originals
        WHERE state='READY' AND (confirmed_at IS NOT NULL OR expires_at > now())""")


def downgrade() -> None:
    raise RuntimeError("Evidence processing is retained provenance; use a forward migration")
