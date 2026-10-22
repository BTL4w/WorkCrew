"""retrieval_web_state

Revision ID: 48c2370fd5f9
Revises: 0044
Create Date: 2026-10-22 22:11:49.394799
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "48c2370fd5f9"
down_revision: str | Sequence[str] | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply the schema change."""
    # Add the referenced same-run key before creating snapshot foreign keys.
    op.create_unique_constraint(
        op.f("uq_tool_invocations_organization_id_agent_run_id_id"),
        "tool_invocations",
        ["organization_id", "agent_run_id", "id"],
    )
    op.create_table(
        "retrieval_run_budgets",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("owner_membership_id", sa.Uuid(), nullable=False),
        sa.Column("orchestration_run_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("worker_id", sa.String(length=255), nullable=False),
        sa.Column("job_attempt", sa.Integer(), nullable=False),
        sa.Column("fencing_token", sa.Uuid(), nullable=False),
        sa.Column("scope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("caps", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reserved", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            (
                "caps ? 'input_tokens' AND reserved ? 'input_tokens' AND "
                "jsonb_typeof(caps->'input_tokens')='number' AND "
                "jsonb_typeof(reserved->'input_tokens')='number' AND "
                "(caps->>'input_tokens')::int BETWEEN 0 AND 12000 AND "
                "(reserved->>'input_tokens')::int BETWEEN 0 AND "
                "(caps->>'input_tokens')::int"
            ),
            name=op.f("ck_retrieval_run_budgets_cap_input_tokens"),
        ),
        sa.CheckConstraint(
            (
                "caps ? 'model_attempts' AND reserved ? 'model_attempts' AND "
                "jsonb_typeof(caps->'model_attempts')='number' AND "
                "jsonb_typeof(reserved->'model_attempts')='number' AND "
                "(caps->>'model_attempts')::int BETWEEN 0 AND 2 AND "
                "(reserved->>'model_attempts')::int BETWEEN 0 AND "
                "(caps->>'model_attempts')::int"
            ),
            name=op.f("ck_retrieval_run_budgets_cap_model_attempts"),
        ),
        sa.CheckConstraint(
            "caps ? 'nodes' AND reserved ? 'nodes' "
            "AND jsonb_typeof(caps->'nodes')='number' "
            "AND jsonb_typeof(reserved->'nodes')='number' "
            "AND (caps->>'nodes')::int BETWEEN 0 AND 12 "
            "AND (reserved->>'nodes')::int BETWEEN 0 AND (caps->>'nodes')::int",
            name=op.f("ck_retrieval_run_budgets_cap_nodes"),
        ),
        sa.CheckConstraint(
            (
                "caps ? 'output_tokens' AND reserved ? 'output_tokens' AND "
                "jsonb_typeof(caps->'output_tokens')='number' AND "
                "jsonb_typeof(reserved->'output_tokens')='number' AND "
                "(caps->>'output_tokens')::int BETWEEN 0 AND 3200 AND "
                "(reserved->>'output_tokens')::int BETWEEN 0 AND "
                "(caps->>'output_tokens')::int"
            ),
            name=op.f("ck_retrieval_run_budgets_cap_output_tokens"),
        ),
        sa.CheckConstraint(
            (
                "caps ? 'page_reads' AND reserved ? 'page_reads' AND "
                "jsonb_typeof(caps->'page_reads')='number' AND "
                "jsonb_typeof(reserved->'page_reads')='number' AND "
                "(caps->>'page_reads')::int BETWEEN 0 AND 3 AND "
                "(reserved->>'page_reads')::int BETWEEN 0 AND "
                "(caps->>'page_reads')::int"
            ),
            name=op.f("ck_retrieval_run_budgets_cap_page_reads"),
        ),
        sa.CheckConstraint(
            (
                "caps ? 'provider_requests' AND reserved ? 'provider_requests' AND "
                "jsonb_typeof(caps->'provider_requests')='number' AND "
                "jsonb_typeof(reserved->'provider_requests')='number' AND "
                "(caps->>'provider_requests')::int BETWEEN 0 AND 8 AND "
                "(reserved->>'provider_requests')::int BETWEEN 0 AND "
                "(caps->>'provider_requests')::int"
            ),
            name=op.f("ck_retrieval_run_budgets_cap_provider_requests"),
        ),
        sa.CheckConstraint(
            "caps ? 'queries' AND reserved ? 'queries' "
            "AND jsonb_typeof(caps->'queries')='number' "
            "AND jsonb_typeof(reserved->'queries')='number' "
            "AND (caps->>'queries')::int BETWEEN 0 AND 2 "
            "AND (reserved->>'queries')::int BETWEEN 0 AND (caps->>'queries')::int",
            name=op.f("ck_retrieval_run_budgets_cap_queries"),
        ),
        sa.CheckConstraint(
            (
                "caps ? 'tool_calls' AND reserved ? 'tool_calls' AND "
                "jsonb_typeof(caps->'tool_calls')='number' AND "
                "jsonb_typeof(reserved->'tool_calls')='number' AND "
                "(caps->>'tool_calls')::int BETWEEN 0 AND 8 AND "
                "(reserved->>'tool_calls')::int BETWEEN 0 AND "
                "(caps->>'tool_calls')::int"
            ),
            name=op.f("ck_retrieval_run_budgets_cap_tool_calls"),
        ),
        sa.CheckConstraint(
            "deadline>started_at AND deadline<=started_at+interval '25 seconds'",
            name=op.f("ck_retrieval_run_budgets_deadline"),
        ),
        sa.CheckConstraint(
            "expires_at>started_at AND expires_at<=started_at+interval '30 days'",
            name=op.f("ck_retrieval_run_budgets_expiry"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(caps)='object' AND jsonb_typeof(reserved)='object'",
            name=op.f("ck_retrieval_run_budgets_counter_objects"),
        ),
        sa.CheckConstraint(
            "version>=1 AND job_attempt>=1", name=op.f("ck_retrieval_run_budgets_versions")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "agent_run_id"],
            ["agent_runs.organization_id", "agent_runs.id"],
            name=op.f("fk_retrieval_run_budgets_organization_id_agent_run_id_agent_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["assistant_jobs.organization_id", "assistant_jobs.id"],
            name=op.f("fk_retrieval_run_budgets_organization_id_job_id_assistant_jobs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "orchestration_run_id"],
            ["orchestration_runs.organization_id", "orchestration_runs.id"],
            name=op.f(
                "fk_retrieval_run_budgets_organization_id_orchestration_run_id_orchestration_runs"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "owner_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            name=op.f("fk_retrieval_run_budgets_organization_id_owner_membership_id_memberships"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id", "agent_run_id", name=op.f("pk_retrieval_run_budgets")
        ),
        sa.UniqueConstraint(
            "organization_id",
            "agent_run_id",
            "owner_membership_id",
            name=op.f("uq_retrieval_run_budgets_organization_id_agent_run_id_owner_membership_id"),
        ),
    )
    op.create_table(
        "retrieval_attempt_reservations",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("owner_membership_id", sa.Uuid(), nullable=False),
        sa.Column("operation_key", sa.String(length=200), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("fencing_token", sa.Uuid(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("charged", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="RESERVED", nullable=False),
        sa.Column("dispatched", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "result_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("cost_usd", sa.Numeric(precision=18, scale=9), nullable=True),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('SEARCH','PAGE_READ','TOOL','NODE','MODEL')",
            name=op.f("ck_retrieval_attempt_reservations_kind"),
        ),
        sa.CheckConstraint(
            "request_hash ~ '^[a-f0-9]{64}$'",
            name=op.f("ck_retrieval_attempt_reservations_fingerprint"),
        ),
        sa.CheckConstraint(
            "status IN ('RESERVED','COMPLETED','UNKNOWN')",
            name=op.f("ck_retrieval_attempt_reservations_status"),
        ),
        sa.CheckConstraint(
            "attempt_no BETWEEN 1 AND 2", name=op.f("ck_retrieval_attempt_reservations_attempt")
        ),
        sa.CheckConstraint(
            "cost_usd IS NULL OR cost_usd>=0", name=op.f("ck_retrieval_attempt_reservations_cost")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "agent_run_id", "owner_membership_id"],
            [
                "retrieval_run_budgets.organization_id",
                "retrieval_run_budgets.agent_run_id",
                "retrieval_run_budgets.owner_membership_id",
            ],
            name=op.f(
                "fk_retrieval_attempt_reservations_organization_id_agent_run_id_owner_mem"
                "bership_id_retrieval_run_budgets"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id", "id", name=op.f("pk_retrieval_attempt_reservations")
        ),
        sa.UniqueConstraint(
            "organization_id",
            "agent_run_id",
            "operation_key",
            "attempt_no",
            name=op.f(
                "uq_retrieval_attempt_reservations_organization_id_agent_run_id_operation"
                "_key_attempt_no"
            ),
        ),
    )
    op.create_table(
        "retrieval_source_snapshots",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_membership_id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("tool_invocation_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_url", sa.String(length=2048), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "expires_at>observed_at AND expires_at<=observed_at+interval '30 days'",
            name=op.f("ck_retrieval_source_snapshots_expiry"),
        ),
        sa.CheckConstraint(
            "fingerprint ~ '^[a-f0-9]{64}$'", name=op.f("ck_retrieval_source_snapshots_fingerprint")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload)='object' AND octet_length(payload::text)<=300000",
            name=op.f("ck_retrieval_source_snapshots_payload_bound"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "agent_run_id", "owner_membership_id"],
            [
                "retrieval_run_budgets.organization_id",
                "retrieval_run_budgets.agent_run_id",
                "retrieval_run_budgets.owner_membership_id",
            ],
            name=op.f(
                "fk_retrieval_source_snapshots_organization_id_agent_run_id_owner_members"
                "hip_id_retrieval_run_budgets"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "agent_run_id", "tool_invocation_id"],
            [
                "tool_invocations.organization_id",
                "tool_invocations.agent_run_id",
                "tool_invocations.id",
            ],
            name=op.f(
                "fk_retrieval_source_snapshots_organization_id_agent_run_id_tool_invocati"
                "on_id_tool_invocations"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id", "id", name=op.f("pk_retrieval_source_snapshots")
        ),
        sa.UniqueConstraint(
            "organization_id",
            "agent_run_id",
            "tool_invocation_id",
            "fingerprint",
            "canonical_url",
            name=op.f(
                "uq_retrieval_source_snapshots_organization_id_agent_run_id_tool_invocati"
                "on_id_fingerprint_canonical_url"
            ),
        ),
    )
    _install_guards()
    # ### end Alembic commands ###


def downgrade() -> None:
    raise RuntimeError("Disable I3 to roll back behavior; preserve evidence and paid reservations")


def _install_guards() -> None:
    op.execute("""CREATE FUNCTION retrieval_owner_scope(p_org uuid,p_owner uuid,p_run uuid)
      RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      SELECT p_org=nullif(current_setting('app.organization_id',true),'')::uuid
        AND p_owner=nullif(current_setting('app.membership_id',true),'')::uuid
        AND EXISTS(SELECT 1 FROM public.memberships m JOIN public.users u ON u.id=m.user_id
          WHERE m.organization_id=p_org AND m.id=p_owner AND m.is_active AND u.is_active)
        AND EXISTS(SELECT 1 FROM public.agent_runs a JOIN public.orchestration_runs r
          ON r.organization_id=a.organization_id AND r.id=a.orchestration_run_id
          JOIN public.assistant_turns t ON t.organization_id=r.organization_id AND t.id=r.turn_id
          JOIN public.assistant_conversations c
            ON c.organization_id=t.organization_id AND c.id=t.conversation_id
          WHERE a.organization_id=p_org AND a.id=p_run AND r.actor_membership_id=p_owner
          AND c.owner_membership_id=p_owner AND c.status='ACTIVE')
      $$""")
    op.execute("REVOKE ALL ON FUNCTION retrieval_owner_scope(uuid,uuid,uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION retrieval_owner_scope(uuid,uuid,uuid) TO app_runtime")
    for table in (
        "retrieval_run_budgets",
        "retrieval_attempt_reservations",
        "retrieval_source_snapshots",
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        predicate = "retrieval_owner_scope(organization_id,owner_membership_id,agent_run_id)"
        if table == "retrieval_run_budgets":
            predicate += (
                " AND EXISTS(SELECT 1 FROM agent_runs a WHERE a.organization_id="
                "retrieval_run_budgets.organization_id AND a.id=retrieval_run_bu"
                "dgets.agent_run_id AND a.orchestration_run_id=retrieval_run_bud"
                "gets.orchestration_run_id) AND EXISTS(SELECT 1 FROM assistant_j"
                "obs j WHERE j.organization_id=retrieval_run_budgets.organizatio"
                "n_id AND j.id=retrieval_run_budgets.job_id AND j.orchestration_"
                "run_id=retrieval_run_budgets.orchestration_run_id AND j.request"
                "er_membership_id=retrieval_run_budgets.owner_membership_id)"
            )
        op.execute(
            f"CREATE POLICY retrieval_owner ON {table} TO app_runtime "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
        op.execute(f"ALTER TABLE {table} OWNER TO migration_owner")
        op.execute(f"REVOKE ALL ON {table} FROM app_runtime")
        op.execute(f"GRANT SELECT,INSERT ON {table} TO app_runtime")
    op.execute(
        "GRANT UPDATE(reserved,version,worker_id,job_attempt,fencing_token) ON retrieval_"
        "run_budgets TO app_runtime"
    )
    op.execute(
        "GRANT UPDATE(status,dispatched,result_metadata,cost_usd,completed_at) ON retriev"
        "al_attempt_reservations TO app_runtime"
    )
    op.execute("""CREATE FUNCTION protect_retrieval_budget() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE counter text;
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'retrieval usage is durable'; END IF;
      IF (to_jsonb(NEW)-ARRAY['reserved','version','worker_id','job_attempt','fencing_token'])
         IS DISTINCT FROM
         (to_jsonb(OLD)-ARRAY['reserved','version','worker_id','job_attempt','fencing_token'])
         OR NEW.version<>OLD.version+1 OR NEW.job_attempt<OLD.job_attempt
         OR ((NEW.fencing_token<>OLD.fencing_token OR NEW.worker_id<>OLD.worker_id)
             AND NEW.job_attempt<=OLD.job_attempt)
      THEN RAISE EXCEPTION 'retrieval identity and deadlines are immutable'; END IF;
      FOR counter IN SELECT jsonb_object_keys(OLD.reserved) LOOP
        IF NOT (NEW.reserved ? counter)
           OR (NEW.reserved->>counter)::int<(OLD.reserved->>counter)::int
        THEN RAISE EXCEPTION 'retrieval counters cannot reset'; END IF;
      END LOOP;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER retrieval_budget_guard BEFORE UPDATE OR DELETE ON retrieval_run_b"
        "udgets FOR EACH ROW EXECUTE FUNCTION protect_retrieval_budget()"
    )
    op.execute("""CREATE FUNCTION protect_retrieval_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' OR OLD.status<>'RESERVED'
        THEN RAISE EXCEPTION 'retrieval observation is immutable'; END IF;
      IF (to_jsonb(NEW)-ARRAY['status','dispatched','result_metadata','cost_usd','completed_at'])
         IS DISTINCT FROM
         (to_jsonb(OLD)-ARRAY['status','dispatched','result_metadata','cost_usd','completed_at'])
         OR (OLD.dispatched AND NOT NEW.dispatched)
      THEN RAISE EXCEPTION 'retrieval receipt is immutable'; END IF;
      IF NEW.status='RESERVED' AND (OLD.dispatched OR NOT NEW.dispatched
         OR NEW.result_metadata<>OLD.result_metadata
         OR NEW.cost_usd IS DISTINCT FROM OLD.cost_usd
         OR NEW.completed_at IS DISTINCT FROM OLD.completed_at)
      THEN RAISE EXCEPTION 'only one dispatch is permitted'; END IF;
      IF NEW.status<>'RESERVED' AND NEW.completed_at IS NULL
        THEN RAISE EXCEPTION 'completion timestamp required'; END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER retrieval_attempt_guard BEFORE UPDATE OR DELETE ON retrieval_atte"
        "mpt_reservations FOR EACH ROW EXECUTE FUNCTION protect_retrieval_attempt()"
    )
    op.execute("""CREATE FUNCTION protect_retrieval_snapshot() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'retrieval source is immutable'; END $$""")
    op.execute(
        "CREATE TRIGGER retrieval_snapshot_guard BEFORE UPDATE OR DELETE ON retrieval_sou"
        "rce_snapshots FOR EACH ROW EXECUTE FUNCTION protect_retrieval_snapshot()"
    )
