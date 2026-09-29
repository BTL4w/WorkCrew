"""Metadata contract tests for the identity and organization schema."""

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

from app.core.database import Base
from app.modules.assistant.adapters import database_models as assistant_models
from app.modules.audit.adapters import database_models as audit_models
from app.modules.identity.adapters import database_models as identity_models
from app.modules.organization.adapters import database_models as organization_models
from app.modules.people_capacity.adapters import database_models as people_capacity_models
from app.modules.progress.adapters import evidence_models, progress_models
from app.modules.work.adapters import database_models as work_models

_MODEL_MODULES = (
    assistant_models,
    audit_models,
    identity_models,
    evidence_models,
    progress_models,
    organization_models,
    people_capacity_models,
    work_models,
)


def test_active_phase_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == {
        "evidence_originals",
        "weekly_plan_baselines",
        "organizations",
        "users",
        "memberships",
        "auth_sessions",
        "audit_events",
        "projects",
        "idempotency_records",
        "tasks",
        "task_status_transitions",
        "goals",
        "milestones",
        "project_weeks",
        "task_dependencies",
        "acceptance_criteria",
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
        "assistant_conversations",
        "assistant_messages",
        "assistant_turns",
        "orchestration_runs",
        "agent_runs",
        "agent_handoffs",
        "agent_checkpoints",
        "agent_context_references",
        "skill_invocations",
        "tool_invocations",
        "agent_model_invocations",
        "assistant_events",
        "assistant_jobs",
        "skills",
        "skill_versions",
        "person_skills",
        "skill_evidence",
        "work_outcome_evidence",
        "capacity_entries",
        "leave_entries",
        "team_requirement_sets",
        "team_requirement_versions",
        "team_requirement_items",
        "team_requirement_task_sources",
        "recommendations",
        "recommendation_versions",
        "candidate_scores",
        "recommendation_selections",
        "recommendation_feedback",
        "recommendation_decisions",
        "project_team_memberships",
    }


def test_tenant_tables_require_organization_id() -> None:
    for table_name in ("memberships", "auth_sessions", "audit_events"):
        column = Base.metadata.tables[table_name].c.organization_id
        assert column.nullable is False


def test_membership_has_tenant_scoped_uniqueness() -> None:
    memberships = Base.metadata.tables["memberships"]
    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in memberships.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("organization_id", "id") in unique_columns
    assert ("organization_id", "user_id") in unique_columns


def test_auth_session_uses_composite_tenant_foreign_key() -> None:
    auth_sessions = Base.metadata.tables["auth_sessions"]
    foreign_keys = [
        constraint
        for constraint in auth_sessions.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    ]

    assert len(foreign_keys) == 1
    assert tuple(foreign_keys[0].column_keys) == ("organization_id", "membership_id")
    assert tuple(element.target_fullname for element in foreign_keys[0].elements) == (
        "memberships.organization_id",
        "memberships.id",
    )


def test_audit_event_actor_uses_composite_tenant_foreign_key() -> None:
    audit_events = Base.metadata.tables["audit_events"]
    foreign_keys = [
        constraint
        for constraint in audit_events.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    ]

    assert len(foreign_keys) == 1
    assert tuple(foreign_keys[0].column_keys) == (
        "organization_id",
        "actor_membership_id",
    )
    assert tuple(element.target_fullname for element in foreign_keys[0].elements) == (
        "memberships.organization_id",
        "memberships.id",
    )
