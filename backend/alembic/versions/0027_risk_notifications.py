"""AI risk snapshots, durable refresh jobs, reviews and notification feed."""

from collections.abc import Sequence

from alembic import op

revision: str = "0027"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE risk_assessments (
    task_id UUID NOT NULL,
    cause_id UUID NOT NULL,
    input_hash VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    id UUID NOT NULL,
    organization_id UUID NOT NULL,
    CONSTRAINT pk_risk_assessments PRIMARY KEY (id),
    CONSTRAINT uq_risk_assessments_organization_id_id UNIQUE (organization_id, id),
    CONSTRAINT fk_risk_assessments_organization_id_task_id_tasks FOREIGN KEY(organization_id,
task_id) REFERENCES tasks (organization_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_risk_assessments_organization_id_task_id_cause_id UNIQUE (organization_id,
task_id, cause_id)
)"""
    )
    op.execute(
        """
CREATE INDEX ix_risk_assessments_task ON risk_assessments (organization_id, task_id,
created_at, id)
        """
    )
    op.execute(
        """CREATE TABLE risk_refresh_jobs (
    task_id UUID NOT NULL,
    actor_membership_id UUID NOT NULL,
    cause_id UUID NOT NULL,
    input_hash VARCHAR(64) NOT NULL,
    state VARCHAR(16) NOT NULL,
    attempts INTEGER NOT NULL,
    lease_until TIMESTAMP WITH TIME ZONE,
    deadline TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    id UUID NOT NULL,
    organization_id UUID NOT NULL,
    CONSTRAINT pk_risk_refresh_jobs PRIMARY KEY (id),
    CONSTRAINT uq_risk_refresh_jobs_organization_id_id UNIQUE (organization_id, id),
    CONSTRAINT fk_risk_refresh_jobs_organization_id_task_id_tasks FOREIGN KEY(organization_id,
task_id) REFERENCES tasks (organization_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_risk_refresh_jobs_organization_id_actor_membership_i_31fc FOREIGN
KEY(organization_id, actor_membership_id) REFERENCES memberships (organization_id, id) ON
DELETE RESTRICT,
    CONSTRAINT uq_risk_refresh_jobs_organization_id_task_id_cause_id UNIQUE (organization_id,
task_id, cause_id),
    CONSTRAINT ck_risk_refresh_jobs_attempts CHECK (attempts BETWEEN 0 AND 3),
    CONSTRAINT ck_risk_refresh_jobs_state CHECK (state IN ('PENDING','RUNNING','DONE','FAILED'))
)"""
    )
    op.execute(
        """
CREATE INDEX ix_risk_jobs_claim ON risk_refresh_jobs (organization_id, state, lease_until,
created_at)
        """
    )
    op.execute(
        """CREATE TABLE risk_review_events (
    risk_id UUID NOT NULL,
    actor_membership_id UUID NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    id UUID NOT NULL,
    organization_id UUID NOT NULL,
    CONSTRAINT pk_risk_review_events PRIMARY KEY (id),
    CONSTRAINT uq_risk_review_events_organization_id_id UNIQUE (organization_id, id),
    CONSTRAINT fk_risk_review_events_organization_id_risk_id_risk_assessments FOREIGN
KEY(organization_id, risk_id) REFERENCES risk_assessments (organization_id, id) ON DELETE
RESTRICT,
    CONSTRAINT fk_risk_review_events_organization_id_actor_membership__e9a7 FOREIGN
KEY(organization_id, actor_membership_id) REFERENCES memberships (organization_id, id) ON
DELETE RESTRICT
)"""
    )
    op.execute(
        """
CREATE INDEX ix_risk_reviews ON risk_review_events (organization_id, risk_id, created_at, id)
        """
    )
    op.execute(
        """CREATE TABLE risk_notifications (
    task_id UUID NOT NULL,
    risk_id UUID NOT NULL,
    recipient_membership_id UUID NOT NULL,
    dedup_key VARCHAR(160) NOT NULL,
    payload JSONB NOT NULL,
    read BOOLEAN NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    id UUID NOT NULL,
    organization_id UUID NOT NULL,
    CONSTRAINT pk_risk_notifications PRIMARY KEY (id),
    CONSTRAINT uq_risk_notifications_organization_id_id UNIQUE (organization_id, id),
    CONSTRAINT fk_risk_notifications_organization_id_task_id_tasks FOREIGN KEY(organization_id,
task_id) REFERENCES tasks (organization_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_risk_notifications_organization_id_recipient_members_ed25 FOREIGN
KEY(organization_id, recipient_membership_id) REFERENCES memberships (organization_id, id) ON
DELETE RESTRICT,
    CONSTRAINT fk_risk_notifications_organization_id_risk_id_risk_assessments FOREIGN
KEY(organization_id, risk_id) REFERENCES risk_assessments (organization_id, id) ON DELETE
RESTRICT,
    CONSTRAINT uq_risk_notifications_organization_id_recipient_members_0038 UNIQUE
(organization_id, recipient_membership_id, dedup_key)
)"""
    )
    op.execute(
        """
CREATE INDEX ix_risk_notifications_feed ON risk_notifications (organization_id,
recipient_membership_id, created_at, id)
        """
    )
    tenant = "organization_id=nullif(current_setting('app.organization_id',true),'')::uuid"
    member = "nullif(current_setting('app.membership_id',true),'')::uuid"
    manager_template = (
        "EXISTS (SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id "
        f"WHERE m.organization_id={{table}}.organization_id AND m.id={member} "
        "AND m.is_active AND u.is_active AND m.role IN ('MANAGER','ADMIN'))"
    )
    for table in (
        "risk_assessments",
        "risk_review_events",
        "risk_notifications",
        "risk_refresh_jobs",
    ):
        manager = manager_template.format(table=table)
        predicate = tenant if table == "risk_refresh_jobs" else f"{tenant} AND {manager}"
        if table == "risk_notifications":
            predicate += f" AND recipient_membership_id={member}"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY risk_scope ON {table} TO app_runtime "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
        mutable = table in ("risk_refresh_jobs", "risk_notifications")
        op.execute(f"GRANT SELECT,INSERT{',UPDATE' if mutable else ''} ON {table} TO app_runtime")
        if not mutable:
            op.execute(
                f"CREATE TRIGGER risk_fact_immutable BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION guard_daily_reporting_fact()"
            )
    op.execute("""CREATE FUNCTION guard_risk_notification() RETURNS trigger LANGUAGE plpgsql AS
$$ BEGIN
      IF TG_OP='DELETE' OR
ROW(NEW.id,NEW.organization_id,NEW.task_id,NEW.risk_id,NEW.recipient_membership_id,
NEW.dedup_key,NEW.payload,NEW.created_at)
IS DISTINCT FROM
ROW(OLD.id,OLD.organization_id,OLD.task_id,OLD.risk_id,OLD.recipient_membership_id,
OLD.dedup_key,OLD.payload,OLD.created_at)
OR (OLD.read AND NOT NEW.read) THEN RAISE EXCEPTION 'immutable risk notification'; END IF;
RETURN NEW; END $$""")
    op.execute(
        """
CREATE TRIGGER risk_notification_immutable BEFORE UPDATE OR DELETE ON risk_notifications FOR
EACH ROW EXECUTE FUNCTION guard_risk_notification()
        """
    )


def downgrade() -> None:
    raise RuntimeError("Use a forward migration to preserve risk review/audit history")
