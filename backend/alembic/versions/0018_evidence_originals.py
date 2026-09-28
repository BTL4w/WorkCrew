"""Private evidence upload reservations, immutable metadata and owner-scoped RLS.

Revision ID: 0018
Revises: 0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evidence_originals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("uploader_membership_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("storage_key", sa.String(160), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("sha256", sa.String(64)),
        sa.Column("byte_length", sa.Integer()),
        sa.Column("mime_type", sa.String(100)),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_id", sa.Uuid(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["organization_id", "uploader_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "id", "version"),
        sa.UniqueConstraint("organization_id", "uploader_membership_id", "idempotency_key"),
        sa.UniqueConstraint("organization_id", "storage_key"),
        sa.CheckConstraint("state IN ('RESERVED', 'READY', 'EXPIRED')", name="state"),
        sa.CheckConstraint("version = 1", name="version"),
        sa.CheckConstraint(
            "state != 'READY' OR (sha256 IS NOT NULL AND byte_length > 0 "
            "AND mime_type IS NOT NULL)",
            name="ready_metadata",
        ),
    )
    op.create_index(
        "ix_evidence_originals_owner",
        "evidence_originals",
        ["organization_id", "uploader_membership_id", "id"],
    )
    op.create_index(
        "ix_evidence_originals_expiry",
        "evidence_originals",
        ["organization_id", "expires_at", "lease_until"],
    )
    op.execute("ALTER TABLE evidence_originals ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE evidence_originals FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY evidence_owner ON evidence_originals FOR ALL TO app_runtime
        USING (organization_id = nullif(current_setting('app.organization_id', true), '')::uuid
          AND uploader_membership_id = nullif(current_setting('app.membership_id', true), '')::uuid)
        WITH CHECK (organization_id = nullif(current_setting('app.organization_id', true), '')::uuid
          AND uploader_membership_id = nullif(current_setting('app.membership_id', true), '')::uuid)
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE ON evidence_originals TO app_runtime")
    op.execute("""
        CREATE FUNCTION guard_evidence_original() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF ROW(NEW.organization_id, NEW.uploader_membership_id, NEW.id, NEW.version,
                 NEW.storage_key, NEW.filename, NEW.idempotency_key,
                 NEW.uploaded_at, NEW.expires_at)
             IS DISTINCT FROM
             ROW(OLD.organization_id, OLD.uploader_membership_id, OLD.id, OLD.version,
                 OLD.storage_key, OLD.filename, OLD.idempotency_key,
                 OLD.uploaded_at, OLD.expires_at)
             OR (OLD.state IN ('READY', 'EXPIRED') AND
                ROW(NEW.sha256, NEW.byte_length, NEW.mime_type) IS DISTINCT FROM
                ROW(OLD.sha256, OLD.byte_length, OLD.mime_type))
             OR (OLD.state = 'EXPIRED' AND NEW.state != 'EXPIRED')
             OR (OLD.state = 'READY' AND NEW.state = 'RESERVED')
             OR (OLD.confirmed_at IS NOT NULL
                 AND NEW.confirmed_at IS DISTINCT FROM OLD.confirmed_at)
             OR (NEW.confirmed_at IS NOT NULL AND NEW.state != 'READY') THEN
            RAISE EXCEPTION 'immutable evidence metadata';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER evidence_original_immutable BEFORE UPDATE ON evidence_originals "
        "FOR EACH ROW EXECUTE FUNCTION guard_evidence_original()"
    )


def downgrade() -> None:
    # Never erase retained business originals during an application rollback.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM evidence_originals) THEN
            RAISE EXCEPTION 'evidence exists; retain revision 0018';
        END IF;
    END $$""")
    op.drop_table("evidence_originals")
    op.execute("DROP FUNCTION guard_evidence_original()")
