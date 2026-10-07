"""Bounded operational-copy retention; business history is untouched.

Revision ID: 0041
Revises: 0040
"""

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None

# Frozen migration inventory; keep historical migrations independent of application imports.
FIELDS = {
    "assistant_turns": {"objective": ""},
    "orchestration_runs": {"execution_plan": {}, "checkpoint": {}},
    "agent_runs": {"typed_input": {}, "typed_output": None},
    "agent_handoffs": {"objective": "", "typed_input": {}, "context_references": []},
    "agent_checkpoints": {"typed_state": {}},
    "skill_invocations": {"typed_input": {}, "typed_output": None},
    "tool_invocations": {"typed_input": {}, "typed_output": None, "context_references": []},
    "assistant_events": {"public_payload": {}},
    "assistant_jobs": {"payload": {}},
    "workflow_runs": {"input_goal_text": "", "error_message": None},
    "workflow_checkpoints": {"state": {}},
    "workflow_events": {"public_payload": {}},
    "workflow_jobs": {"payload": {}, "last_error": None},
    "context_references": {"provenance_notes": None},
    "evidence_processing_jobs": {"result": None},
    "evidence_segments": {"content": {}},
    "evaluation_candidates": {"context": None},
}

PARENTS = {
    "agent_runs": ("orchestration_runs", "orchestration_run_id"),
    "agent_handoffs": ("orchestration_runs", "orchestration_run_id"),
    "agent_checkpoints": ("orchestration_runs", "orchestration_run_id"),
    "skill_invocations": ("agent_runs", "agent_run_id"),
    "tool_invocations": ("agent_runs", "agent_run_id"),
    "workflow_checkpoints": ("workflow_runs", "workflow_run_id"),
    "workflow_events": ("workflow_runs", "workflow_run_id"),
    "workflow_jobs": ("workflow_runs", "workflow_run_id"),
    "context_references": ("workflow_runs", "workflow_run_id"),
    "evidence_segments": ("evidence_processing_jobs", "job_id"),
    "assistant_events": ("orchestration_runs", "orchestration_run_id"),
    "assistant_jobs": ("orchestration_runs", "orchestration_run_id"),
}


def upgrade() -> None:
    op.execute("""DO $$ BEGIN
      IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='app_retention') THEN
        CREATE ROLE app_retention NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT;
      ELSIF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='app_retention'
          AND (rolsuper OR rolbypassrls OR rolcanlogin OR rolinherit)) THEN
        RAISE EXCEPTION 'unsafe retention role configuration';
      END IF;
    END $$""")
    op.execute("GRANT app_retention TO CURRENT_USER")
    op.create_table(
        "ai_retention_payloads",
        sa.Column("organization_id", sa.Uuid(), primary_key=True),
        sa.Column("storage_kind", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), primary_key=True),
        sa.Column("classification", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.Uuid()),
        sa.Column("fence", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("purged_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            (
                "classification IN ('RAW_CONTEXT','REDACTED_TRACE') AND fence>=0 AND "
                "expires_at>=created_at AND expires_at<=created_at + CASE WHEN "
                "classification='RAW_CONTEXT' THEN interval '30 days' ELSE interval '90 "
                "days' END"
            ),
            name="retention",
        ),
    )
    op.create_index(
        "ix_ai_retention_payloads_expired",
        "ai_retention_payloads",
        ["organization_id", "expires_at", "storage_kind", "owner_id"],
        postgresql_where=sa.text("purged_at IS NULL"),
    )
    tenant = "organization_id=nullif(current_setting('app.organization_id',true),'')::uuid"
    op.execute("ALTER TABLE ai_retention_payloads ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ai_retention_payloads FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY ai_retention_scope ON ai_retention_payloads "
        f"TO app_runtime,app_retention USING ({tenant}) WITH CHECK ({tenant})"
    )
    op.execute("GRANT SELECT,INSERT ON ai_retention_payloads TO app_runtime")
    op.execute("GRANT SELECT,INSERT,UPDATE ON ai_retention_payloads TO app_retention")
    op.execute("GRANT USAGE ON SCHEMA public TO app_retention")
    op.execute("""CREATE FUNCTION guard_retention_metadata() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='INSERT' AND pg_trigger_depth()<2 AND current_user IN
('app_runtime','app_retention') THEN
        RAISE EXCEPTION 'retention locators registered only by source triggers';
      END IF;
      IF TG_OP='UPDATE' AND ((to_jsonb(NEW)-'purged_at'-'fence') IS DISTINCT FROM
        (to_jsonb(OLD)-'purged_at'-'fence') OR OLD.purged_at IS NOT NULL OR
        NEW.fence<>OLD.fence+1 OR NEW.purged_at IS NULL OR
NEW.purged_at<LEAST(OLD.expires_at,OLD.created_at+make_interval(days=>COALESCE(nullif(current_setting('app.ai_raw_retention_days',true),'')::int,30))))
THEN
        RAISE EXCEPTION 'retention metadata immutable';
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER retention_metadata BEFORE INSERT OR UPDATE ON "
        "ai_retention_payloads FOR EACH ROW EXECUTE FUNCTION "
        "guard_retention_metadata()"
    )
    op.execute("""CREATE FUNCTION retention_uuid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE AS $$ SELECT COALESCE(jsonb_typeof(value)='string' AND
    value#>>'{}' ~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$',
      false)
    $$""")
    op.execute("""CREATE FUNCTION retention_positive_int(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE AS $$ SELECT COALESCE(jsonb_typeof(value)='number' AND
      value::text ~ '^[1-9][0-9]{0,9}$',false) $$""")
    op.execute("""CREATE FUNCTION retention_business_metadata(kind text, data jsonb) RETURNS boolean
    LANGUAGE plpgsql IMMUTABLE AS $$
    DECLARE body jsonb; allowed text[]; decision_shape boolean:=false; code jsonb;
    BEGIN
      IF kind='workflow_jobs' THEN
        body:=data->'payload';
        IF data->>'last_error' IS NOT NULL AND
           data->>'last_error' !~ '^[A-Z][A-Z0-9_]{0,63}$' THEN RETURN false; END IF;
        IF data->>'job_type'='proposal.revalidate' AND body->>'instruction'='REVALIDATE' THEN
          allowed:=ARRAY['instruction','proposal_id','proposal_version'];
        ELSIF data->>'job_type'='planning.finalize' AND
              body->>'instruction'='FINALIZE_MANAGER_DECISION' THEN
          allowed:=ARRAY['instruction','proposal_id','proposal_version','approval_id',
                         'decision','checkpoint_sequence','project_id']; decision_shape:=true;
          IF NOT retention_positive_int(body->'checkpoint_sequence') THEN RETURN false; END IF;
        ELSE RETURN false; END IF;
      ELSIF kind='workflow_checkpoints' THEN
        body:=data->'state'; decision_shape:=true;
        IF data->>'node' IS DISTINCT FROM 'completed' OR
           body->>'stage' IS DISTINCT FROM 'COMPLETED' THEN RETURN false; END IF;
        allowed:=ARRAY['stage','decision','proposal_id','proposal_version','approval_id','project_id'];
      ELSIF kind='workflow_events' THEN
        body:=data->'public_payload';
        IF data->>'event_type'='workflow.completed' AND body->>'status'='COMPLETED' THEN
          allowed:=ARRAY['status','decision','proposal_id','proposal_version','approval_id',
                         'project_id']; decision_shape:=true;
        ELSIF data->>'event_type'='proposal.validating' THEN
          allowed:=ARRAY['proposal_id','version'];
        ELSIF data->>'event_type' IN ('proposal.ready','proposal.validation_failed') THEN
          allowed:=ARRAY['proposal_id','version','approval_id','can_approve','error_codes'];
          IF jsonb_typeof(body->'can_approve') IS DISTINCT FROM 'boolean' OR
             jsonb_typeof(body->'error_codes') IS DISTINCT FROM 'array' THEN RETURN false; END IF;
          FOR code IN SELECT value FROM jsonb_array_elements(body->'error_codes') LOOP
            IF jsonb_typeof(code) IS DISTINCT FROM 'string' OR
               code#>>'{}' !~ '^[A-Z][A-Z0-9_]{0,63}$' THEN RETURN false; END IF;
          END LOOP;
          IF body->'approval_id' IS DISTINCT FROM 'null'::jsonb AND
             NOT retention_uuid(body->'approval_id') THEN RETURN false; END IF;
        ELSE RETURN false; END IF;
      ELSE RETURN false; END IF;
      IF jsonb_typeof(body) IS DISTINCT FROM 'object' THEN RETURN false; END IF;
      IF EXISTS(SELECT 1 FROM jsonb_object_keys(body) k WHERE NOT k=ANY(allowed)) OR
         NOT retention_uuid(body->'proposal_id') THEN RETURN false; END IF;
      IF kind='workflow_events' AND NOT decision_shape THEN
        IF NOT retention_positive_int(body->'version') THEN RETURN false; END IF;
      ELSIF NOT retention_positive_int(body->'proposal_version') THEN RETURN false; END IF;
      IF decision_shape AND (NOT retention_uuid(body->'approval_id') OR
         body->>'decision' IS NULL OR body->>'decision' NOT IN ('APPROVE','REJECT'))
         THEN RETURN false; END IF;
      IF body ? 'project_id' AND body->'project_id' IS DISTINCT FROM 'null'::jsonb AND
         NOT retention_uuid(body->'project_id') THEN RETURN false; END IF;
      RETURN true;
    END $$""")
    # Both helpers are invoker functions. Neither elevates roles or bypasses RLS.
    op.execute("""CREATE FUNCTION register_ai_payload() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE born timestamptz; deadline timestamptz; parent uuid; parent_purged timestamptz;
            data jsonb; days int;
    BEGIN
      data:=to_jsonb(NEW);
      IF retention_business_metadata(TG_TABLE_NAME,data) THEN RETURN NEW; END IF;
      days:=LEAST(30,COALESCE(nullif(current_setting('app.ai_raw_retention_days',true),'')::int,30));
      born:=COALESCE((data->>'created_at')::timestamptz,(data->>'occurred_at')::timestamptz);
      IF TG_NARGS=2 THEN
        parent:=(data->>TG_ARGV[1])::uuid;
        IF parent IS NOT NULL THEN
          SELECT created_at,expires_at,purged_at INTO born,deadline,parent_purged
          FROM ai_retention_payloads WHERE organization_id=NEW.organization_id
            AND storage_kind=TG_ARGV[0] AND owner_id=parent;
          IF born IS NULL THEN RAISE EXCEPTION 'missing tenant retention parent'; END IF;
        END IF;
      END IF;
      deadline:=LEAST(COALESCE(deadline,born+interval '30 days'),
                     born+make_interval(days=>days));
      IF TG_TABLE_NAME='evaluation_candidates' THEN
        deadline:=LEAST(deadline,(data->>'expires_at')::timestamptz);
      END IF;
      IF current_user='app_runtime' AND
         (deadline<=clock_timestamp() OR parent_purged IS NOT NULL) THEN
        RAISE EXCEPTION 'CONTEXT_EXPIRED';
      END IF;
      INSERT INTO ai_retention_payloads
        (organization_id,storage_kind,owner_id,classification,created_at,expires_at,run_id)
        VALUES(NEW.organization_id,TG_TABLE_NAME,NEW.id,'RAW_CONTEXT',born,deadline,parent);
      RETURN NEW;
    END $$""")
    op.execute("""CREATE FUNCTION guard_ai_payload() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE locator ai_retention_payloads; fields text[]; clean jsonb;
            deadline timestamptz; old_data jsonb; new_data jsonb; key text;
    BEGIN
      SELECT * INTO locator FROM ai_retention_payloads
        WHERE organization_id=OLD.organization_id AND storage_kind=TG_TABLE_NAME
          AND owner_id=OLD.id;
      fields:=string_to_array(TG_ARGV[0],','); clean:=TG_ARGV[1]::jsonb;
      old_data:=to_jsonb(OLD); new_data:=to_jsonb(NEW);
      deadline:=LEAST(locator.expires_at,locator.created_at+make_interval(days=>LEAST(30,
        COALESCE(nullif(current_setting('app.ai_raw_retention_days',true),'')::int,30))));
      IF locator.owner_id IS NULL THEN
        IF current_user='app_retention' OR
           NOT retention_business_metadata(TG_TABLE_NAME,old_data) OR
           NOT retention_business_metadata(TG_TABLE_NAME,new_data) THEN
          RAISE EXCEPTION 'unregistered or invalid retention metadata';
        END IF;
        IF (TG_TABLE_NAME='workflow_jobs' AND new_data->'payload' IS DISTINCT FROM
            old_data->'payload') OR
           (TG_TABLE_NAME='workflow_checkpoints' AND new_data->'state' IS DISTINCT FROM
            old_data->'state') OR
           (TG_TABLE_NAME='workflow_events' AND new_data->'public_payload' IS DISTINCT FROM
            old_data->'public_payload') THEN RAISE EXCEPTION 'business pointers immutable'; END IF;
        IF TG_TABLE_NAME='workflow_jobs' AND new_data->>'last_error' IS NOT NULL AND
           new_data->>'last_error' !~ '^[A-Z][A-Z0-9_]{0,63}$' THEN
          RAISE EXCEPTION 'unsafe deterministic job diagnostic';
        END IF;
        RETURN NEW;
      END IF;
      IF current_user='app_retention' THEN
        IF locator.owner_id IS NULL OR deadline>COALESCE(
           nullif(current_setting('app.retention_now',true),'')::timestamptz,clock_timestamp())
          OR (new_data-fields-ARRAY['status','safe_error_code','stop_reason',
               'locked_by','locked_by_worker_id','lease_until']) IS DISTINCT FROM
             (old_data-fields-ARRAY['status','safe_error_code','stop_reason',
               'locked_by','locked_by_worker_id','lease_until'])
          OR EXISTS(SELECT 1 FROM jsonb_each(clean) p
            WHERE new_data->p.key IS DISTINCT FROM p.value AND NOT (
              ((TG_TABLE_NAME='workflow_jobs' AND p.key='last_error') OR
               (TG_TABLE_NAME='workflow_runs' AND p.key='error_message'))
              AND new_data->>p.key='CONTEXT_EXPIRED')) THEN
          RAISE EXCEPTION 'invalid retention scrub';
        END IF;
        IF new_data->'status' IS DISTINCT FROM old_data->'status'
           AND (new_data->>'status'<>'FAILED' OR old_data->>'status' NOT IN
                ('QUEUED','RUNNING','AWAITING_INPUT','AWAITING_HUMAN')) THEN
          RAISE EXCEPTION 'invalid retention terminal state';
        END IF;
        FOREACH key IN ARRAY ARRAY['safe_error_code','stop_reason'] LOOP
          IF new_data->key IS DISTINCT FROM old_data->key
             AND new_data->>key IS DISTINCT FROM 'CONTEXT_EXPIRED' THEN
            RAISE EXCEPTION 'invalid retention error';
          END IF;
        END LOOP;
        FOREACH key IN ARRAY ARRAY['locked_by','locked_by_worker_id','lease_until'] LOOP
          IF new_data->key IS DISTINCT FROM old_data->key AND new_data->>key IS NOT NULL THEN
            RAISE EXCEPTION 'invalid retention lease';
          END IF;
        END LOOP;
      ELSIF locator.purged_at IS NOT NULL OR deadline<=clock_timestamp() THEN
        IF TG_TABLE_NAME='evaluation_candidates' AND
           (new_data->'status' IS DISTINCT FROM old_data->'status' OR
            new_data->'version' IS DISTINCT FROM old_data->'version') THEN
          RAISE EXCEPTION 'CONTEXT_EXPIRED';
        END IF;
        IF (new_data-fields) IS DISTINCT FROM (old_data-fields)
           AND TG_TABLE_NAME NOT IN ('workflow_runs','evaluation_candidates') THEN
          RAISE EXCEPTION 'CONTEXT_EXPIRED';
        END IF;
        IF EXISTS(SELECT 1 FROM unnest(fields) p
          WHERE new_data->p IS DISTINCT FROM old_data->p) THEN
          RAISE EXCEPTION 'CONTEXT_EXPIRED';
        END IF;
      END IF;
      RETURN NEW;
    END $$""")
    import json

    # Roots first, then children, so parent deadlines are inherited during backfill.
    ordered = (
        [t for t in FIELDS if t not in PARENTS]
        + [t for t in FIELDS if t in PARENTS and t not in ("skill_invocations", "tool_invocations")]
        + ["skill_invocations", "tool_invocations"]
    )
    for table in ordered:
        columns = ",".join(FIELDS[table])
        op.execute(f"GRANT SELECT ON {table} TO app_retention")
        op.execute(f"GRANT UPDATE ({columns}) ON {table} TO app_retention")
        op.execute(
            f"CREATE POLICY {table}_retention ON {table} TO app_retention "
            f"USING ({tenant}) WITH CHECK ({tenant})"
        )
        parent = PARENTS.get(table)
        born = "s.occurred_at" if table == "assistant_events" else "s.created_at"
        if table == "evidence_segments":
            born = "p.created_at"
        join = ""
        run = "NULL::uuid"
        deadline = f"{born}+interval '30 days'"
        if parent:
            pt, col = parent
            join = (
                "LEFT JOIN ai_retention_payloads p ON p.organization_id=s.organization_id "
                f"AND p.storage_kind='{pt}' AND p.owner_id=s.{col}"
            )
            run = f"s.{col}"
            if table != "evidence_segments":
                born = f"COALESCE(p.created_at,{born})"
            deadline = f"COALESCE(p.expires_at,{born}+interval '30 days')"
        if table == "evaluation_candidates":
            deadline = f"LEAST(s.expires_at,{deadline})"
        op.execute(
            "INSERT INTO ai_retention_payloads(organization_id,storage_kind,owner_id,"
            "classification,created_at,expires_at,run_id) "
            f"SELECT s.organization_id,'{table}',s.id,'RAW_CONTEXT',"
            f"{born},{deadline},{run} FROM {table} s {join}"
            + f" WHERE NOT retention_business_metadata('{table}',to_jsonb(s))"
        )
        args = "" if not parent else f"'{parent[0]}','{parent[1]}'"
        op.execute(
            f"CREATE TRIGGER register_{table}_retention AFTER INSERT ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION register_ai_payload({args})"
        )
        clean = json.dumps(FIELDS[table]).replace("'", "''")
        op.execute(
            f"CREATE TRIGGER a_guard_{table}_retention BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION guard_ai_payload('{columns}','{clean}')"
        )
    # Existing immutable triggers still constrain application writes. Only the dedicated
    # scrub role gets a narrow exception, checked by a_guard_* above and column grants.
    op.execute("""CREATE OR REPLACE FUNCTION protect_assistant_invocation_terminal() RETURNS trigger
    LANGUAGE plpgsql AS $$
      BEGIN
        IF current_user='app_retention' THEN RETURN NEW; END IF;
        IF OLD.status<>'RUNNING' OR NEW.status='RUNNING' THEN RAISE EXCEPTION 'assistant
invocation terminal update required'; END IF;
        IF (to_jsonb(NEW)-ARRAY['typed_output','status','safe_error_code','completed_at']) IS
DISTINCT FROM
           (to_jsonb(OLD)-ARRAY['typed_output','status','safe_error_code','completed_at']) THEN
          RAISE EXCEPTION 'assistant invocation identity and input are immutable'; END IF;
        RETURN NEW;
      END $$""")
    # Preserve the old provenance guard for all ordinary writes; retention changes only goal text.
    op.execute("""CREATE OR REPLACE FUNCTION protect_workflow_run_provenance() RETURNS trigger
    LANGUAGE plpgsql AS $$
      BEGIN
        IF current_user='app_retention' THEN RETURN NEW; END IF;
        IF (to_jsonb(NEW)-ARRAY['status','error_message','version','updated_at']) IS DISTINCT FROM
           (to_jsonb(OLD)-ARRAY['status','error_message','version','updated_at']) THEN
          RAISE EXCEPTION 'workflow run provenance immutable'; END IF;
        RETURN NEW;
      END $$""")
    op.execute("""CREATE FUNCTION scrub_retired_evidence_segment() RETURNS trigger
    LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP='UPDATE' AND current_user='app_retention' THEN RETURN NEW; END IF;
        RAISE EXCEPTION 'evidence segment immutable';
      END $$""")
    op.execute("DROP TRIGGER evidence_segment_immutable ON evidence_segments")
    op.execute(
        "CREATE TRIGGER evidence_segment_immutable BEFORE UPDATE OR DELETE ON "
        "evidence_segments FOR EACH ROW EXECUTE FUNCTION "
        "scrub_retired_evidence_segment()"
    )
    op.execute("""CREATE OR REPLACE FUNCTION guard_evidence_processing_job() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
      IF current_user='app_retention' THEN RETURN NEW; END IF;
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
    for table, columns in {
        "orchestration_runs": "status,safe_error_code,stop_reason",
        "workflow_runs": "status",
        "assistant_turns": "status,safe_error_code",
        "agent_runs": "status,safe_error_code",
        "skill_invocations": "status,safe_error_code",
        "tool_invocations": "status,safe_error_code",
        "assistant_jobs": "status,safe_error_code,locked_by,lease_until",
        "workflow_jobs": "status,locked_by_worker_id,lease_until",
        "report_generation_jobs": "state,safe_error_code,fence,lease_until,lease_owner",
    }.items():
        op.execute(f"GRANT SELECT,UPDATE ({columns}) ON {table} TO app_retention")
        if table == "report_generation_jobs":
            op.execute(
                f"CREATE POLICY {table}_retention ON {table} TO app_retention "
                f"USING ({tenant}) WITH CHECK ({tenant})"
            )
    for table in ("audit_events", "outbox_events"):
        op.execute(f"GRANT INSERT ON {table} TO app_retention")
        op.execute(
            f"CREATE POLICY {table}_retention ON {table} TO app_retention WITH CHECK ({tenant})"
        )

    op.execute("""CREATE FUNCTION guard_retention_report_job() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE expiry timestamptz; purged timestamptz;
    BEGIN
      IF current_user<>'app_retention' THEN RETURN NEW; END IF;
      SELECT LEAST(expires_at,created_at+make_interval(days=>LEAST(30,
        COALESCE(nullif(current_setting('app.ai_raw_retention_days',true),'')::int,30)))),purged_at
        INTO expiry,purged FROM ai_retention_payloads
        WHERE organization_id=OLD.organization_id AND storage_kind='orchestration_runs'
          AND owner_id=OLD.orchestration_run_id;
      IF expiry IS NULL OR expiry>COALESCE(
        nullif(current_setting('app.retention_now',true),'')::timestamptz,clock_timestamp())
        OR OLD.state NOT IN ('QUEUED','RUNNING') OR NEW.state<>'AI_UNAVAILABLE'
        OR NEW.safe_error_code IS DISTINCT FROM 'CONTEXT_EXPIRED'
        OR NEW.fence<>OLD.fence+1 OR NEW.lease_owner IS NOT NULL OR NEW.lease_until IS NOT NULL
        OR (to_jsonb(NEW)-ARRAY['state','safe_error_code','fence','lease_owner','lease_until'])
           IS DISTINCT FROM
           (to_jsonb(OLD)-ARRAY['state','safe_error_code','fence','lease_owner','lease_until']) THEN
        RAISE EXCEPTION 'invalid retention report transition';
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER retention_report_job BEFORE UPDATE ON report_generation_jobs "
        "FOR EACH ROW EXECUTE FUNCTION guard_retention_report_job()"
    )


def downgrade() -> None:
    raise RuntimeError(
        "Retention scrubbing is irreversible; restore/recovery needs explicit authorization"
    )
