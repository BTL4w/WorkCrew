"""Restore evidence finalization while retaining the tenant retention boundary."""

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0041 accidentally excluded this existing terminal output column. The earlier
    # a_guard trigger and column grants still constrain the dedicated cleanup role.
    op.execute("""CREATE OR REPLACE FUNCTION protect_assistant_invocation_terminal()
    RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE allowed_columns text[] :=
      ARRAY['typed_output','status','safe_error_code','completed_at'];
    BEGIN
      IF current_user='app_retention' THEN RETURN NEW; END IF;
      IF OLD.status<>'RUNNING' OR NEW.status='RUNNING' THEN
        RAISE EXCEPTION 'assistant invocation terminal update required';
      END IF;
      IF TG_TABLE_NAME='tool_invocations' THEN
        allowed_columns := array_append(allowed_columns,'context_references');
      END IF;
      IF (to_jsonb(NEW)-allowed_columns) IS DISTINCT FROM
         (to_jsonb(OLD)-allowed_columns) THEN
        RAISE EXCEPTION 'assistant invocation identity and input are immutable';
      END IF;
      RETURN NEW;
    END $$""")


def downgrade() -> None:
    op.execute("""CREATE OR REPLACE FUNCTION protect_assistant_invocation_terminal()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF current_user='app_retention' THEN RETURN NEW; END IF;
      IF OLD.status<>'RUNNING' OR NEW.status='RUNNING' THEN
        RAISE EXCEPTION 'assistant invocation terminal update required';
      END IF;
      IF (to_jsonb(NEW)-ARRAY['typed_output','status','safe_error_code','completed_at'])
         IS DISTINCT FROM
         (to_jsonb(OLD)-ARRAY['typed_output','status','safe_error_code','completed_at']) THEN
        RAISE EXCEPTION 'assistant invocation identity and input are immutable';
      END IF;
      RETURN NEW;
    END $$""")
