"""Reconcile AI planning runs lifecycle states, metadata, outbox schema and least privilege grants.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Data Backfill
    op.execute(
        sa.text(
            "UPDATE workflow_runs "
            "SET status = 'WAITING_FOR_DECISION' "
            "WHERE status = 'PAUSED_FOR_APPROVAL';"
        )
    )
    op.execute(
        sa.text(
            "UPDATE workflow_runs "
            "SET status = 'FAILED', "
            "error_message = COALESCE("
            "error_message, 'Workflow run cancelled') "
            "WHERE status = 'CANCELLED';"
        )
    )
    op.execute(
        sa.text("UPDATE proposals SET status = 'READY_FOR_DECISION' WHERE status = 'READY';")
    )
    op.execute(sa.text("UPDATE proposals SET status = 'STALE' WHERE status = 'SUPERSEDED';"))

    # 2. Drop legacy constraints & legacy indexes
    op.execute(
        sa.text("ALTER TABLE workflow_runs DROP CONSTRAINT IF EXISTS ck_workflow_runs_status")
    )
    op.execute(sa.text("ALTER TABLE proposals DROP CONSTRAINT IF EXISTS ck_proposals_status"))
    op.execute(
        sa.text(
            "ALTER TABLE outbox_events "
            "DROP CONSTRAINT IF EXISTS "
            "outbox_events_organization_id_event_id_key"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE outbox_events "
            "DROP CONSTRAINT IF EXISTS "
            "uq_outbox_events_organization_id_event_id"
        )
    )
    op.execute(sa.text("DROP INDEX IF EXISTS ix_outbox_events_status"))

    # 3. Add physical check constraints
    op.create_check_constraint(
        "ck_workflow_runs_status",
        "workflow_runs",
        "status IN ("
        "'QUEUED', 'RUNNING', 'NEEDS_INPUT', "
        "'WAITING_FOR_DECISION', 'COMPLETED', 'FAILED')",
    )
    op.create_check_constraint(
        "ck_proposals_status",
        "proposals",
        "status IN ('DRAFT', 'VALIDATING', 'READY_FOR_DECISION', 'APPROVED', 'REJECTED', 'STALE')",
    )

    # 4. Add proposal_versions metadata columns
    op.add_column(
        "proposal_versions",
        sa.Column(
            "field_provenance",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    _validation_default = (
        '\'{"status": "UNKNOWN", "is_valid": null, "errors": [], "warnings": []}\'::jsonb'
    )
    op.add_column(
        "proposal_versions",
        sa.Column(
            "validation_result",
            postgresql.JSONB(),
            server_default=sa.text(_validation_default),
            nullable=False,
        ),
    )
    op.add_column(
        "proposal_versions",
        sa.Column(
            "source_reference_snapshot",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "proposal_versions",
        sa.Column(
            "workflow_version",
            sa.String(length=50),
            server_default="UNKNOWN",
            nullable=False,
        ),
    )
    op.add_column(
        "proposal_versions",
        sa.Column(
            "prompt_version",
            sa.String(length=50),
            server_default="UNKNOWN",
            nullable=False,
        ),
    )
    op.add_column(
        "proposal_versions",
        sa.Column(
            "schema_version",
            sa.String(length=50),
            server_default="UNKNOWN",
            nullable=False,
        ),
    )
    op.add_column(
        "proposal_versions",
        sa.Column(
            "model_reference",
            sa.String(length=100),
            server_default="UNKNOWN",
            nullable=False,
        ),
    )
    op.add_column(
        "proposal_versions",
        sa.Column(
            "verifier_version",
            sa.String(length=50),
            server_default="UNKNOWN",
            nullable=False,
        ),
    )
    op.add_column(
        "proposal_versions",
        sa.Column(
            "creator_type",
            sa.String(length=50),
            server_default="UNKNOWN",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_proposal_versions_creator_type",
        "proposal_versions",
        "creator_type IN ('AI_SYSTEM', 'HUMAN_MANAGER', 'UNKNOWN')",
    )

    # 5. Adjust outbox_events schema
    op.alter_column(
        "outbox_events",
        "processed_at",
        new_column_name="published_at",
    )
    op.add_column(
        "outbox_events",
        sa.Column(
            "max_attempts",
            sa.Integer(),
            server_default="3",
            nullable=False,
        ),
    )
    op.add_column(
        "outbox_events",
        sa.Column(
            "last_error_code",
            sa.String(length=100),
            nullable=True,
        ),
    )
    op.add_column(
        "outbox_events",
        sa.Column(
            "envelope_version",
            sa.String(length=50),
            server_default="1.0",
            nullable=False,
        ),
    )
    op.add_column(
        "outbox_events",
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.add_column(
        "outbox_events",
        sa.Column(
            "locked_by_worker_id",
            sa.String(length=100),
            nullable=True,
        ),
    )
    op.add_column(
        "outbox_events",
        sa.Column(
            "lease_until",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_outbox_events_status",
        "outbox_events",
        "status IN ('PENDING', 'DISPATCHING', 'DISPATCHED', 'FAILED')",
    )
    op.create_unique_constraint(
        "uq_outbox_events_organization_event",
        "outbox_events",
        ["organization_id", "event_id"],
    )
    op.create_index(
        "ix_outbox_events_queue",
        "outbox_events",
        [
            "organization_id",
            "status",
            "available_at",
            "lease_until",
            "id",
        ],
    )

    # 6. Apply least-privilege grants to app_runtime
    op.execute(
        sa.text(
            "GRANT SELECT, INSERT, UPDATE, DELETE "
            "ON workflow_checkpoints, workflow_jobs "
            "TO app_runtime;"
        )
    )
    op.execute(sa.text("GRANT SELECT, INSERT ON workflow_runs TO app_runtime;"))
    op.execute(
        sa.text(
            "GRANT UPDATE "
            "(status, error_message, version, updated_at) "
            "ON workflow_runs TO app_runtime;"
        )
    )
    op.execute(sa.text("GRANT SELECT, INSERT, UPDATE ON proposals TO app_runtime;"))
    op.execute(sa.text("GRANT SELECT, INSERT ON approvals, outbox_events TO app_runtime;"))
    op.execute(
        sa.text(
            "GRANT UPDATE ("
            "status, decided_by_membership_id, "
            "decision_reason, decided_at, "
            "version, updated_at"
            ") ON approvals TO app_runtime;"
        )
    )
    op.execute(
        sa.text(
            "GRANT UPDATE ("
            "status, attempt_count, max_attempts, "
            "available_at, published_at, last_error, "
            "last_error_code, locked_by_worker_id, "
            "lease_until"
            ") ON outbox_events TO app_runtime;"
        )
    )
    op.execute(
        sa.text(
            "GRANT SELECT, INSERT "
            "ON proposal_versions, workflow_events, "
            "model_invocations, context_references "
            "TO app_runtime;"
        )
    )


def downgrade() -> None:
    # 1. Drop check constraints
    op.drop_constraint(
        "ck_proposal_versions_creator_type",
        "proposal_versions",
        type_="check",
    )
    op.drop_constraint(
        "ck_outbox_events_status",
        "outbox_events",
        type_="check",
    )
    op.drop_constraint(
        "ck_proposals_status",
        "proposals",
        type_="check",
    )
    op.drop_constraint(
        "ck_workflow_runs_status",
        "workflow_runs",
        type_="check",
    )

    # 2. Outbox index and constraint rollback
    op.drop_index(
        "ix_outbox_events_queue",
        table_name="outbox_events",
    )
    op.drop_constraint(
        "uq_outbox_events_organization_event",
        "outbox_events",
        type_="unique",
    )
    op.execute(
        sa.text(
            "ALTER TABLE outbox_events "
            "ADD CONSTRAINT "
            "outbox_events_organization_id_event_id_key "
            "UNIQUE (organization_id, event_id);"
        )
    )
    op.create_index(
        "ix_outbox_events_status",
        "outbox_events",
        [
            "organization_id",
            "status",
            "available_at",
            "id",
        ],
    )

    # 3. Outbox columns rollback
    op.drop_column("outbox_events", "lease_until")
    op.drop_column("outbox_events", "locked_by_worker_id")
    op.drop_column("outbox_events", "occurred_at")
    op.drop_column("outbox_events", "envelope_version")
    op.drop_column("outbox_events", "last_error_code")
    op.drop_column("outbox_events", "max_attempts")
    op.alter_column(
        "outbox_events",
        "published_at",
        new_column_name="processed_at",
    )

    # 4. proposal_versions columns rollback
    op.drop_column("proposal_versions", "creator_type")
    op.drop_column("proposal_versions", "verifier_version")
    op.drop_column("proposal_versions", "model_reference")
    op.drop_column("proposal_versions", "schema_version")
    op.drop_column("proposal_versions", "prompt_version")
    op.drop_column("proposal_versions", "workflow_version")
    op.drop_column(
        "proposal_versions",
        "source_reference_snapshot",
    )
    op.drop_column(
        "proposal_versions",
        "validation_result",
    )
    op.drop_column(
        "proposal_versions",
        "field_provenance",
    )

    # 5. Restore exact 0006-era grants
    _all_tables = (
        "workflow_runs",
        "workflow_checkpoints",
        "proposals",
        "proposal_versions",
        "approvals",
        "workflow_jobs",
        "workflow_events",
        "model_invocations",
        "context_references",
        "outbox_events",
    )
    for table in _all_tables:
        op.execute(sa.text(f'REVOKE ALL ON "{table}" FROM app_runtime'))

    # Deletable: full CRUD
    for table in ("workflow_checkpoints", "workflow_jobs"):
        op.execute(sa.text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{table}" TO app_runtime'))

    # Stateful: SELECT, INSERT, UPDATE
    op.execute(sa.text("GRANT SELECT, INSERT, UPDATE ON proposals TO app_runtime"))

    # Update-only: SELECT, INSERT + column UPDATE
    for table in (
        "workflow_runs",
        "approvals",
        "outbox_events",
    ):
        op.execute(sa.text(f'GRANT SELECT, INSERT ON "{table}" TO app_runtime'))

    op.execute(
        sa.text(
            "GRANT UPDATE "
            "(status, error_message, version, updated_at) "
            "ON workflow_runs TO app_runtime"
        )
    )
    op.execute(
        sa.text(
            "GRANT UPDATE ("
            "status, decided_by_membership_id, "
            "decision_reason, decided_at, "
            "version, updated_at"
            ") ON approvals TO app_runtime"
        )
    )
    op.execute(
        sa.text(
            "GRANT UPDATE ("
            "status, attempt_count, "
            "available_at, processed_at, last_error"
            ") ON outbox_events TO app_runtime"
        )
    )

    # Append-only: SELECT, INSERT only
    for table in (
        "proposal_versions",
        "workflow_events",
        "model_invocations",
        "context_references",
    ):
        op.execute(sa.text(f'GRANT SELECT, INSERT ON "{table}" TO app_runtime'))
