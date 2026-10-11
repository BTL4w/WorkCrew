"""SQLAlchemy models for AI planning run persistence."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

import app.modules.organization.adapters.database_models as _org_models
import app.modules.work.adapters.database_models as _work_models
from app.core.database import Base

_DEPENDENT_MODELS = (_org_models, _work_models)


class WorkflowRunModel(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "requested_by_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "id"),
        CheckConstraint(
            "verifier_version ~ '[^[:space:]]'",
            name="ck_workflow_runs_verifier_version",
        ),
        CheckConstraint(
            "status IN ("
            "'QUEUED', 'RUNNING', 'NEEDS_INPUT', "
            "'DRAFT_AVAILABLE', 'WAITING_FOR_DECISION', 'COMPLETED', 'FAILED')",
            name="status",
        ),
        Index("ix_workflow_runs_project", "organization_id", "project_id", "id"),
        Index("ix_workflow_runs_status", "organization_id", "status", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    project_id: Mapped[UUID | None] = mapped_column(nullable=True)
    requested_by_membership_id: Mapped[UUID]
    status: Mapped[str] = mapped_column(String(50), default="QUEUED", server_default="QUEUED")
    workflow_name: Mapped[str] = mapped_column(String(100))
    workflow_version: Mapped[str] = mapped_column(String(50))
    verifier_version: Mapped[str] = mapped_column(String(50))
    input_goal_text: Mapped[str] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkflowCheckpointModel(Base):
    __tablename__ = "workflow_checkpoints"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workflow_run_id"],
            ["workflow_runs.organization_id", "workflow_runs.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "workflow_run_id", "sequence"),
        Index("ix_workflow_checkpoints_run", "organization_id", "workflow_run_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    workflow_run_id: Mapped[UUID]
    node: Mapped[str] = mapped_column(String(100))
    sequence: Mapped[int] = mapped_column(Integer)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProposalModel(Base):
    __tablename__ = "proposals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workflow_run_id"],
            ["workflow_runs.organization_id", "workflow_runs.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "id", "current_version_number"],
            [
                "proposal_versions.organization_id",
                "proposal_versions.proposal_id",
                "proposal_versions.version_number",
            ],
            name="fk_proposals_current_version",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["organization_id", "id", "approval_id"],
            ["approvals.organization_id", "approvals.proposal_id", "approvals.id"],
            name="fk_proposals_approval",
            ondelete="SET NULL",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["organization_id", "id", "superseded_approval_id"],
            ["approvals.organization_id", "approvals.proposal_id", "approvals.id"],
            name="fk_proposals_superseded_approval",
            ondelete="SET NULL",
            use_alter=True,
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "id", "current_version_number"),
        CheckConstraint(
            "status IN ("
            "'DRAFT', 'VALIDATING', 'READY_FOR_DECISION', "
            "'APPROVED', 'REJECTED', 'STALE')",
            name="status",
        ),
        Index("ix_proposals_run", "organization_id", "workflow_run_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    workflow_run_id: Mapped[UUID]
    status: Mapped[str] = mapped_column(String(50), default="DRAFT", server_default="DRAFT")
    current_version_number: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    approval_id: Mapped[UUID | None] = mapped_column(nullable=True)
    superseded_approval_id: Mapped[UUID | None] = mapped_column(nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProposalVersionModel(Base):
    __tablename__ = "proposal_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "source_draft_id", "source_draft_version"],
            [
                "planning_draft_versions.organization_id",
                "planning_draft_versions.draft_id",
                "planning_draft_versions.version_number",
            ],
            name="fk_proposal_versions_source_draft",
        ),
        CheckConstraint(
            "(source_draft_id IS NULL) = (source_draft_version IS NULL)", name="source_draft_pair"
        ),
        ForeignKeyConstraint(
            ["organization_id", "proposal_id"],
            ["proposals.organization_id", "proposals.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "created_by_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "proposal_id", "version_number"),
        CheckConstraint(
            "creator_type IN ('AI_SYSTEM', 'HUMAN_MANAGER', 'UNKNOWN')",
            name="creator_type",
        ),
        Index("ix_proposal_versions_proposal", "organization_id", "proposal_id", "version_number"),
    )

    source_draft_id: Mapped[UUID | None] = mapped_column(nullable=True)
    source_draft_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    proposal_id: Mapped[UUID]
    version_number: Mapped[int] = mapped_column(Integer)
    created_by_membership_id: Mapped[UUID]
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    assumptions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    change_summary: Mapped[str | None] = mapped_column(Text)
    field_provenance: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    validation_result: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text(
            '\'{"status": "UNKNOWN", "is_valid": null, "errors": [], "warnings": []}\'::jsonb'
        ),
    )
    source_reference_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    workflow_version: Mapped[str] = mapped_column(
        String(50),
        default="UNKNOWN",
        server_default="UNKNOWN",
    )
    prompt_version: Mapped[str] = mapped_column(
        String(50),
        default="UNKNOWN",
        server_default="UNKNOWN",
    )
    schema_version: Mapped[str] = mapped_column(
        String(50),
        default="UNKNOWN",
        server_default="UNKNOWN",
    )
    model_reference: Mapped[str] = mapped_column(
        String(100),
        default="UNKNOWN",
        server_default="UNKNOWN",
    )
    verifier_version: Mapped[str] = mapped_column(
        String(50),
        default="UNKNOWN",
        server_default="UNKNOWN",
    )
    creator_type: Mapped[str] = mapped_column(
        String(50),
        default="UNKNOWN",
        server_default="UNKNOWN",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class ApprovalModel(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "proposal_id"],
            ["proposals.organization_id", "proposals.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "proposal_id", "proposal_version_number"],
            [
                "proposal_versions.organization_id",
                "proposal_versions.proposal_id",
                "proposal_versions.version_number",
            ],
            ondelete="CASCADE",
            name="fk_approvals_proposal_version",
        ),
        ForeignKeyConstraint(
            ["organization_id", "decided_by_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "proposal_id", "id"),
        CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'SUPERSEDED')",
            name="ck_approvals_status",
        ),
        CheckConstraint("version >= 1", name="ck_approvals_version"),
        Index("ix_approvals_proposal", "organization_id", "proposal_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    proposal_id: Mapped[UUID]
    proposal_version_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(50), default="PENDING", server_default="PENDING")
    decided_by_membership_id: Mapped[UUID | None] = mapped_column(nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkflowJobModel(Base):
    __tablename__ = "workflow_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workflow_run_id"],
            ["workflow_runs.organization_id", "workflow_runs.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("organization_id", "id"),
        Index(
            "ix_workflow_jobs_queue",
            "organization_id",
            "status",
            "available_at",
            "id",
        ),
        Index(
            "ix_workflow_jobs_lease",
            "organization_id",
            "locked_by_worker_id",
            "lease_until",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    workflow_run_id: Mapped[UUID]
    job_type: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(50), default="QUEUED", server_default="QUEUED")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    locked_by_worker_id: Mapped[str | None] = mapped_column(String(100))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkflowEventModel(Base):
    __tablename__ = "workflow_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workflow_run_id"],
            ["workflow_runs.organization_id", "workflow_runs.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "workflow_run_id", "sequence"),
        Index("ix_workflow_events_run", "organization_id", "workflow_run_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    workflow_run_id: Mapped[UUID]
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(100))
    public_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModelInvocationModel(Base):
    __tablename__ = "model_invocations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workflow_run_id"],
            ["workflow_runs.organization_id", "workflow_runs.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("organization_id", "id"),
        Index("ix_model_invocations_run", "organization_id", "workflow_run_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    workflow_run_id: Mapped[UUID]
    provider: Mapped[str] = mapped_column(String(50))
    model_name: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(50))
    schema_version: Mapped[str] = mapped_column(String(50))
    invocation_key: Mapped[str] = mapped_column(String(100))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(50), default="SUCCESS", server_default="SUCCESS")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ContextReferenceModel(Base):
    __tablename__ = "context_references"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workflow_run_id"],
            ["workflow_runs.organization_id", "workflow_runs.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("organization_id", "id"),
        Index("ix_context_references_run", "organization_id", "workflow_run_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    workflow_run_id: Mapped[UUID]
    resource_type: Mapped[str] = mapped_column(String(50))
    resource_id: Mapped[UUID]
    provenance_notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OutboxEventModel(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "event_id", name="uq_outbox_events_organization_event"),
        CheckConstraint(
            "status IN ('PENDING', 'DISPATCHING', 'DISPATCHED', 'FAILED')",
            name="status",
        ),
        Index(
            "ix_outbox_events_queue",
            "organization_id",
            "status",
            "available_at",
            "lease_until",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    event_id: Mapped[UUID]
    event_type: Mapped[str] = mapped_column(String(100))
    aggregate_type: Mapped[str] = mapped_column(String(100))
    aggregate_id: Mapped[UUID]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(50), default="PENDING", server_default="PENDING")
    envelope_version: Mapped[str] = mapped_column(String(50), default="1.0", server_default="1.0")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error: Mapped[str | None] = mapped_column(Text)
    locked_by_worker_id: Mapped[str | None] = mapped_column(String(100))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlanningDraftModel(Base):
    __tablename__ = "planning_drafts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workflow_run_id"],
            ["workflow_runs.organization_id", "workflow_runs.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "created_by_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "id", "current_version_number"],
            [
                "planning_draft_versions.organization_id",
                "planning_draft_versions.draft_id",
                "planning_draft_versions.version_number",
            ],
            name="fk_planning_drafts_current_version",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "workflow_run_id"),
        CheckConstraint("current_version_number >= 1 AND version >= 1", name="versions"),
        Index("ix_planning_drafts_owner", "organization_id", "created_by_membership_id", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    workflow_run_id: Mapped[UUID]
    created_by_membership_id: Mapped[UUID]
    current_version_number: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlanningDraftVersionModel(Base):
    __tablename__ = "planning_draft_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "draft_id"],
            ["planning_drafts.organization_id", "planning_drafts.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "draft_id", "version_number"),
        CheckConstraint("version_number >= 1", name="version"),
        CheckConstraint("fingerprint ~ '^[a-f0-9]{64}$'", name="fingerprint"),
        Index("ix_planning_draft_versions_draft", "organization_id", "draft_id", "version_number"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    draft_id: Mapped[UUID]
    version_number: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
