"""Expire web snapshots and derived answers without deleting business history."""

from alembic import op

revision = "a1b7c9d3e501"
down_revision = "48c2370fd5f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE FUNCTION retrieval_context_block(block jsonb) RETURNS boolean
      LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,public AS $$
      SELECT COALESCE(block->>'kind'='retrieval_answer' OR
        (block->>'kind'='work_evidence' AND EXISTS(
          SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(block->'evidence')='array'
            THEN block->'evidence' ELSE '[]'::jsonb END) ref
          WHERE ref->>'resource_type'='WEB_SOURCE_SNAPSHOT')),false) $$""")
    op.execute("""CREATE FUNCTION has_retrieval_context(blocks jsonb) RETURNS boolean
      LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,public AS $$
      SELECT EXISTS(SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(blocks)='array'
        THEN blocks ELSE '[]'::jsonb END) block WHERE retrieval_context_block(block)) $$""")
    op.execute(r"""CREATE FUNCTION expired_retrieval_blocks(blocks jsonb) RETURNS jsonb
      LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog,public AS $$
      DECLARE block jsonb; result jsonb:='[]'; clean jsonb;
      BEGIN
        FOR block IN SELECT value FROM jsonb_array_elements(blocks) LOOP
          clean:=block;
          IF retrieval_context_block(block) THEN
            clean:=jsonb_build_object('kind','text',
              'text','Web evidence expired / Nguồn web đã hết hạn.');
            IF block->>'kind'='retrieval_answer' AND block->>'schema_version'='1.0'
              AND retention_uuid(block->'context_run_id')
              AND block->>'observed_at' ~
                '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$'
              AND block->>'outcome' IN ('ANSWER_READY','EVIDENCE_READY','PARTIAL','NOT_FOUND',
                'SOURCE_UNAVAILABLE','DOCUMENTS_NOT_READY','REJECTED') THEN
              clean:=jsonb_build_object('kind','retrieval_answer','schema_version','1.0',
                'context_run_id',block->'context_run_id','outcome',block->'outcome',
                'observed_at',block->'observed_at','claims','[]'::jsonb,'sources','[]'::jsonb,
                'limitations','[]'::jsonb,'availability','EXPIRED');
            END IF;
          END IF;
          result:=result||jsonb_build_array(clean);
        END LOOP;
        RETURN result;
      END $$""")
    op.execute("""CREATE FUNCTION check_retrieval_message(p_org uuid,p_turn uuid,blocks jsonb)
      RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
      DECLARE block jsonb; ref jsonb; source_id uuid; source retrieval_source_snapshots;
        locator ai_retention_payloads; now_at timestamptz;
      BEGIN
        FOR block IN SELECT value FROM jsonb_array_elements(blocks)
          WHERE retrieval_context_block(value) LOOP
          FOR ref IN SELECT value FROM jsonb_array_elements(
            COALESCE(block->'source_refs','[]')||COALESCE(block->'sources','[]')||
            COALESCE(block->'evidence','[]')) LOOP
            IF block->>'kind'='work_evidence' AND
               ref->>'resource_type' IS DISTINCT FROM 'WEB_SOURCE_SNAPSHOT' THEN CONTINUE; END IF;
            source_id:=COALESCE(ref->>'evidence_id',ref->>'resource_id')::uuid;
            IF NOT pg_try_advisory_xact_lock(hashtextextended(
              p_org::text||':retrieval_source_snapshots:'||source_id::text,0)) THEN
              RAISE EXCEPTION 'CONTEXT_EXPIRED'; END IF;
            SELECT * INTO source FROM retrieval_source_snapshots
              WHERE organization_id=p_org AND id=source_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'CONTEXT_EXPIRED'; END IF;
            SELECT * INTO locator FROM ai_retention_payloads WHERE organization_id=p_org
              AND storage_kind='retrieval_source_snapshots' AND owner_id=source_id;
            now_at:=clock_timestamp();
            IF locator.owner_id IS NULL OR locator.purged_at IS NOT NULL OR
              LEAST(locator.expires_at,locator.created_at+make_interval(days=>LEAST(30,
                COALESCE(nullif(current_setting('app.ai_raw_retention_days',true),'')::int,30))))
                <=now_at OR source.expires_at<=now_at OR source.payload='{}'::jsonb OR
              (ref ? 'fingerprint' AND ref->>'fingerprint' IS DISTINCT FROM source.fingerprint) OR
              (block ? 'context_run_id' AND block->>'context_run_id'<>source.agent_run_id::text) OR
              NOT EXISTS(SELECT 1 FROM agent_runs a JOIN orchestration_runs r
                ON r.organization_id=a.organization_id AND r.id=a.orchestration_run_id
                WHERE a.organization_id=p_org AND a.id=source.agent_run_id AND r.turn_id=p_turn)
              THEN RAISE EXCEPTION 'CONTEXT_EXPIRED'; END IF;
          END LOOP;
        END LOOP;
      END $$""")
    op.execute("""CREATE FUNCTION register_retrieval_payload() RETURNS trigger
      LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
      DECLARE born timestamptz; deadline timestamptz; parent uuid;
        parent_kind text; parent_row ai_retention_payloads;
      BEGIN
        IF TG_TABLE_NAME='assistant_messages' THEN
          IF NEW.role<>'ASSISTANT' OR NOT has_retrieval_context(NEW.content_blocks)
            THEN RETURN NEW; END IF;
          parent:=NEW.turn_id; parent_kind:='assistant_turns'; born:=NEW.created_at;
          IF current_user='app_runtime' THEN
            PERFORM check_retrieval_message(NEW.organization_id,NEW.turn_id,NEW.content_blocks);
          END IF;
        ELSE
          parent:=NEW.agent_run_id; parent_kind:='agent_runs'; born:=NEW.observed_at;
        END IF;
        SELECT * INTO parent_row FROM ai_retention_payloads
          WHERE organization_id=NEW.organization_id
          AND storage_kind=parent_kind AND owner_id=parent;
        IF parent_row.owner_id IS NOT NULL THEN born:=LEAST(born,parent_row.created_at); END IF;
        deadline:=LEAST(COALESCE(parent_row.expires_at,born+interval '30 days'),
          born+make_interval(days=>LEAST(30,COALESCE(
            nullif(current_setting('app.ai_raw_retention_days',true),'')::int,30))));
        IF TG_TABLE_NAME='retrieval_source_snapshots' THEN
          deadline:=LEAST(deadline,NEW.expires_at);
        END IF;
        IF current_user='app_runtime' AND (parent_row.owner_id IS NULL OR
          parent_row.purged_at IS NOT NULL OR deadline<=clock_timestamp())
          THEN RAISE EXCEPTION 'CONTEXT_EXPIRED'; END IF;
        INSERT INTO ai_retention_payloads(organization_id,storage_kind,owner_id,classification,
          created_at,expires_at,run_id) VALUES(NEW.organization_id,TG_TABLE_NAME,NEW.id,
          'RAW_CONTEXT',born,deadline,parent);
        RETURN NEW;
      END $$""")
    op.execute("""CREATE FUNCTION guard_retrieval_payload() RETURNS trigger
      LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
      DECLARE locator ai_retention_payloads; old_data jsonb; new_data jsonb; fields text[];
        clean jsonb; deadline timestamptz;
      BEGIN
        old_data:=to_jsonb(OLD); new_data:=to_jsonb(NEW);
        IF TG_TABLE_NAME='assistant_messages' AND
          (old_data->>'role'<>'ASSISTANT' OR
           NOT has_retrieval_context(old_data->'content_blocks')) THEN
          IF TG_OP='UPDATE' AND has_retrieval_context(new_data->'content_blocks') THEN
            RAISE EXCEPTION 'retrieval transcript is append only'; END IF;
          IF TG_OP='DELETE' THEN RETURN OLD; END IF;
          RETURN NEW;
        END IF;
        IF TG_OP='DELETE' OR current_user<>'app_retention'
          THEN RAISE EXCEPTION 'retrieval context is immutable'; END IF;
        SELECT * INTO locator FROM ai_retention_payloads WHERE organization_id=OLD.organization_id
          AND storage_kind=TG_TABLE_NAME AND owner_id=OLD.id;
        deadline:=LEAST(locator.expires_at,locator.created_at+make_interval(days=>LEAST(30,
          COALESCE(nullif(current_setting('app.ai_raw_retention_days',true),'')::int,30))));
        IF locator.owner_id IS NULL OR deadline>COALESCE(
          nullif(current_setting('app.retention_now',true),'')::timestamptz,clock_timestamp())
          THEN RAISE EXCEPTION 'invalid retention scrub'; END IF;
        IF TG_TABLE_NAME='assistant_messages' THEN
          fields:=ARRAY['content_blocks'];
          clean:=jsonb_build_object('content_blocks',expired_retrieval_blocks(OLD.content_blocks));
        ELSE
          fields:=ARRAY['payload','canonical_url'];
          clean:=jsonb_build_object('payload','{}'::jsonb,'canonical_url','urn:expired:'||OLD.id::text);
        END IF;
        IF (new_data-fields) IS DISTINCT FROM (old_data-fields) OR
          EXISTS(SELECT 1 FROM jsonb_each(clean) p WHERE new_data->p.key IS DISTINCT FROM p.value)
          THEN RAISE EXCEPTION 'invalid retention scrub'; END IF;
        RETURN NEW;
      END $$""")
    # The new a_guard validates the exact scrub before this existing immutable guard.
    op.execute("""CREATE OR REPLACE FUNCTION protect_retrieval_snapshot() RETURNS trigger
      LANGUAGE plpgsql AS $$ BEGIN
        IF TG_OP='UPDATE' AND current_user='app_retention' THEN RETURN NEW; END IF;
        RAISE EXCEPTION 'retrieval source is immutable'; END $$""")
    tenant = "organization_id=nullif(current_setting('app.organization_id',true),'')::uuid"
    for table, columns in (
        ("retrieval_source_snapshots", "payload,canonical_url"),
        ("assistant_messages", "content_blocks"),
    ):
        op.execute(f"GRANT SELECT,UPDATE ({columns}) ON {table} TO app_retention")
        op.execute(
            f"CREATE POLICY {table}_web_retention ON {table} TO app_retention "
            f"USING ({tenant}) WITH CHECK ({tenant})"
        )
        op.execute(
            f"CREATE TRIGGER a_guard_{table}_web_retention BEFORE UPDATE OR DELETE "
            f"ON {table} FOR EACH ROW EXECUTE FUNCTION guard_retrieval_payload()"
        )
        op.execute(
            f"CREATE TRIGGER register_{table}_web_retention AFTER INSERT ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION register_retrieval_payload()"
        )
    op.execute(r"""CREATE FUNCTION web_context_source_ids(data jsonb) RETURNS SETOF uuid
      LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,public AS $$
        SELECT (value#>>'{}')::uuid FROM jsonb_path_query(data,'strict $.**.evidence_id') value
          WHERE retention_uuid(value)
        UNION
        SELECT (value->>'resource_id')::uuid FROM jsonb_path_query(data,'strict $.**') value
          WHERE value->>'resource_type'='WEB_SOURCE_SNAPSHOT'
            AND retention_uuid(value->'resource_id')
        UNION
        SELECT split_part(value#>>'{}',':',2)::uuid FROM
          jsonb_path_query(data,'strict $.**.context_reference_ids[*]') value
          WHERE value#>>'{}' ~
            '^web:[a-fA-F0-9]{8}(-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}:[a-f0-9]{64}$'
      $$""")
    op.execute("""CREATE FUNCTION assert_web_sources_live(p_org uuid,data jsonb) RETURNS void
      LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
      DECLARE source ai_retention_payloads; source_id uuid;
      BEGIN
        FOR source_id IN SELECT id FROM web_context_source_ids(data) id ORDER BY id LOOP
          IF NOT pg_try_advisory_xact_lock(hashtextextended(
            p_org::text||':retrieval_source_snapshots:'||source_id::text,0))
            THEN RAISE EXCEPTION 'CONTEXT_EXPIRED'; END IF;
          -- A cursor snapshot may predate a committed purge: re-read under the lock.
          SELECT * INTO source FROM ai_retention_payloads WHERE organization_id=p_org
            AND storage_kind='retrieval_source_snapshots' AND owner_id=source_id;
          IF NOT FOUND THEN CONTINUE; END IF;
          IF source.purged_at IS NOT NULL OR
            LEAST(source.expires_at,source.created_at+make_interval(days=>LEAST(30,
              COALESCE(nullif(current_setting('app.ai_raw_retention_days',true),'')::int,30))))
              <=clock_timestamp() THEN RAISE EXCEPTION 'CONTEXT_EXPIRED'; END IF;
        END LOOP;
      END $$""")
    # New downstream copies inherit the earliest source deadline, even across workflows.
    # No UPDATE grant or security-definer function is needed to shorten a NEW locator.
    op.execute("""CREATE FUNCTION inherit_web_payload_deadline() RETURNS trigger
      LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
      DECLARE data jsonb; born timestamptz; deadline timestamptz; purged boolean;
      BEGIN
        IF NEW.storage_kind NOT IN ('assistant_turns','orchestration_runs','agent_runs',
          'agent_handoffs','agent_checkpoints','skill_invocations','tool_invocations',
          'assistant_events','assistant_jobs','workflow_runs','workflow_checkpoints',
          'workflow_events','workflow_jobs','context_references','evidence_processing_jobs',
          'evidence_segments','evaluation_candidates','assistant_messages',
          'retrieval_source_snapshots') THEN RETURN NEW; END IF;
        EXECUTE format('SELECT to_jsonb(s) FROM %I s WHERE organization_id=$1 AND id=$2',
          NEW.storage_kind) INTO data USING NEW.organization_id,NEW.owner_id;
        IF current_user='app_runtime' THEN
          PERFORM assert_web_sources_live(NEW.organization_id,data);
        END IF;
        SELECT min(p.created_at),min(p.expires_at),bool_or(p.purged_at IS NOT NULL)
          INTO born,deadline,purged FROM ai_retention_payloads p
          WHERE p.organization_id=NEW.organization_id
            AND p.storage_kind='retrieval_source_snapshots'
          AND p.owner_id IN (SELECT web_context_source_ids(data));
        IF born IS NOT NULL THEN
          NEW.created_at:=LEAST(NEW.created_at,born);
          NEW.expires_at:=LEAST(NEW.expires_at,deadline,NEW.created_at+interval '30 days');
          IF current_user='app_runtime' AND (purged OR NEW.expires_at<=clock_timestamp())
            THEN RAISE EXCEPTION 'CONTEXT_EXPIRED'; END IF;
        END IF;
        RETURN NEW;
      END $$""")
    op.execute(
        "CREATE TRIGGER a_inherit_web_deadline BEFORE INSERT ON ai_retention_payloads "
        "FOR EACH ROW EXECUTE FUNCTION inherit_web_payload_deadline()"
    )
    op.execute("""CREATE FUNCTION guard_web_copy_write() RETURNS trigger
      LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
      DECLARE old_data jsonb; new_data jsonb; fields text[];
      BEGIN
        IF current_user<>'app_runtime' THEN RETURN NEW; END IF;
        old_data:=to_jsonb(OLD); new_data:=to_jsonb(NEW); fields:=string_to_array(TG_ARGV[0],',');
        IF EXISTS(SELECT 1 FROM unnest(fields) key
          WHERE new_data->key IS DISTINCT FROM old_data->key) THEN
          PERFORM assert_web_sources_live(NEW.organization_id,new_data);
        END IF;
        RETURN NEW;
      END $$""")
    # Freeze the context columns here; no runtime table/column arguments or extra grants.
    for table, columns in {
        "orchestration_runs": "execution_plan,checkpoint",
        "agent_runs": "typed_input,typed_output",
        "agent_handoffs": "objective,typed_input,context_references",
        "agent_checkpoints": "typed_state",
        "skill_invocations": "typed_input,typed_output",
        "tool_invocations": "typed_input,typed_output,context_references",
        "assistant_events": "public_payload",
        "assistant_jobs": "payload",
        "workflow_runs": "input_goal_text,error_message",
        "workflow_checkpoints": "state",
        "workflow_events": "public_payload",
        "workflow_jobs": "payload,last_error",
        "context_references": "provenance_notes",
    }.items():
        op.execute(
            f"CREATE TRIGGER a_web_copy_write BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION guard_web_copy_write('{columns}')"
        )
    # Original/inherited timestamps: neither deployment nor replay refreshes the TTL.
    op.execute("""INSERT INTO ai_retention_payloads(organization_id,storage_kind,owner_id,
      classification,created_at,expires_at,run_id)
      SELECT s.organization_id,'retrieval_source_snapshots',s.id,'RAW_CONTEXT',
        LEAST(s.observed_at,COALESCE(p.created_at,s.observed_at)),
        LEAST(s.expires_at,COALESCE(p.expires_at,s.observed_at+interval '30 days'),
          LEAST(s.observed_at,COALESCE(p.created_at,s.observed_at))+interval '30 days'),
        s.agent_run_id
      FROM retrieval_source_snapshots s LEFT JOIN ai_retention_payloads p
        ON p.organization_id=s.organization_id AND p.owner_id=s.agent_run_id
        AND p.storage_kind='agent_runs'""")
    op.execute("""INSERT INTO ai_retention_payloads(organization_id,storage_kind,owner_id,
      classification,created_at,expires_at,run_id)
      SELECT s.organization_id,'assistant_messages',s.id,'RAW_CONTEXT',
        LEAST(s.created_at,COALESCE(p.created_at,s.created_at)),
        LEAST(COALESCE(p.expires_at,s.created_at+interval '30 days'),
          LEAST(s.created_at,COALESCE(p.created_at,s.created_at))+interval '30 days'),s.turn_id
      FROM assistant_messages s LEFT JOIN ai_retention_payloads p
        ON p.organization_id=s.organization_id AND p.owner_id=s.turn_id
        AND p.storage_kind='assistant_turns'
      WHERE s.role='ASSISTANT' AND has_retrieval_context(s.content_blocks)""")
    # Trusted transactional backfill; the DDL lock excludes writes until the original guard returns.
    op.execute("ALTER TABLE ai_retention_payloads DISABLE TRIGGER retention_metadata")
    for table in (
        "orchestration_runs",
        "agent_runs",
        "agent_handoffs",
        "agent_checkpoints",
        "skill_invocations",
        "tool_invocations",
        "assistant_events",
        "assistant_jobs",
        "workflow_runs",
        "workflow_checkpoints",
        "workflow_events",
        "workflow_jobs",
        "context_references",
        "assistant_messages",
    ):
        op.execute(f"""UPDATE ai_retention_payloads target SET
          created_at=LEAST(target.created_at,bound.born),
          expires_at=LEAST(target.expires_at,bound.deadline,
            LEAST(target.created_at,bound.born)+interval '30 days')
          FROM (SELECT s.organization_id,s.id,min(source.created_at) born,
            min(source.expires_at) deadline FROM {table} s
            JOIN ai_retention_payloads current_copy ON
              current_copy.organization_id=s.organization_id AND current_copy.owner_id=s.id
              AND current_copy.storage_kind='{table}' AND current_copy.classification='RAW_CONTEXT'
            CROSS JOIN LATERAL web_context_source_ids(to_jsonb(s)) refs(id)
            JOIN ai_retention_payloads source ON source.organization_id=s.organization_id
              AND source.storage_kind='retrieval_source_snapshots' AND source.owner_id=refs.id
            GROUP BY s.organization_id,s.id) bound
          WHERE target.organization_id=bound.organization_id AND target.owner_id=bound.id
            AND target.storage_kind='{table}' AND target.classification='RAW_CONTEXT'""")
    op.execute("ALTER TABLE ai_retention_payloads ENABLE TRIGGER retention_metadata")


def downgrade() -> None:
    raise RuntimeError("Disable retrieval to roll back behavior; never restore purged web context")
