"""Metadata contract tests for the identity and organization schema."""

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

from app.core.database import Base
from app.modules.assistant.adapters import database_models as assistant_models
from app.modules.audit.adapters import database_models as audit_models
from app.modules.automations.adapters import database_models as automation_models
from app.modules.automations.adapters import delivery_models
from app.modules.feedback.adapters import database_models as feedback_models
from app.modules.identity.adapters import database_models as identity_models
from app.modules.organization.adapters import database_models as organization_models
from app.modules.people_capacity.adapters import database_models as people_capacity_models
from app.modules.progress.adapters import (
    assessment_models,
    completion_models,
    daily_update_models,
    evidence_models,
    extraction_models,
    progress_models,
)
from app.modules.reporting.adapters import database_models as reporting_models
from app.modules.work.adapters import database_models as work_models

_MODEL_MODULES = (
    reporting_models,
    feedback_models,
    automation_models,
    delivery_models,
    assessment_models,
    completion_models,
    assistant_models,
    audit_models,
    identity_models,
    daily_update_models,
    evidence_models,
    extraction_models,
    progress_models,
    organization_models,
    people_capacity_models,
    work_models,
)


def test_active_phase_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == {
        "reports",
        "feedback",
        "feedback_outcomes",
        "report_version_verifications",
        "report_metric_snapshots",
        "report_versions",
        "report_generation_jobs",
        "report_generation_usage",
        "report_publications",
        "report_review_decisions",
        "report_snapshot_sources",
        "report_snapshot_receipts",
        "daily_summary_triggers",
        "daily_summary_snapshots",
        "daily_summary_deliveries",
        "automation_schedules",
        "automation_schedule_versions",
        "automation_schedule_recipients",
        "automation_schedule_drafts",
        "reporting_windows",
        "reporting_window_reporters",
        "automation_task_scope_history",
        "task_completion_checks",
        "daily_update_drafts",
        "daily_update_draft_revisions",
        "daily_updates",
        "task_progress_observations",
        "task_actual_projections",
        "work_logs",
        "daily_update_evidence_links",
        "weekly_actual_snapshots",
        "blockers",
        "blocker_transitions",
        "blocker_evidence_links",
        "evidence_originals",
        "evidence_assessments",
        "warning_acknowledgments",
        "evidence_processing_jobs",
        "evidence_segments",
        "weekly_plan_baselines",
        "risk_assessments",
        "risk_refresh_jobs",
        "risk_review_events",
        "risk_notifications",
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
