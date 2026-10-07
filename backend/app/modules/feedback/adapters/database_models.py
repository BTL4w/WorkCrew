"""Append-only, tenant-qualified human feedback and exact edit-verification verdicts."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class FeedbackModel(Base):
    __tablename__ = "feedback"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "report_id", "report_version_id"],
            ["report_versions.organization_id", "report_versions.report_id", "report_versions.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "report_id", "original_version_id"],
            ["report_versions.organization_id", "report_versions.report_id", "report_versions.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "generation_id"],
            ["report_generation_jobs.organization_id", "report_generation_jobs.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "actor_membership_id"],
            ["memberships.organization_id", "memberships.id"],
        ),
        ForeignKeyConstraint(
            [
                "organization_id",
                "report_id",
                "report_version_id",
                "snapshot_hash",
                "actor_membership_id",
                "decision_id",
            ],
            [
                "report_review_decisions.organization_id",
                "report_review_decisions.report_id",
                "report_review_decisions.report_version_id",
                "report_review_decisions.snapshot_hash",
                "report_review_decisions.actor_membership_id",
                "report_review_decisions.id",
            ],
        ),
        CheckConstraint(
            "kind IN ('TERMINAL_QUALITY','ADVISORY') AND "
            "((kind='TERMINAL_QUALITY') = (decision_id IS NOT NULL))",
            name="kind",
        ),
        CheckConstraint("decision IN ('ACCEPT','EDIT','REJECT')", name="decision"),
        Index("ix_feedback_report", "organization_id", "report_id", "created_at", "id"),
        Index(
            "uq_feedback_terminal_generation",
            "organization_id",
            "generation_id",
            unique=True,
            postgresql_where=text("kind='TERMINAL_QUALITY'"),
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    report_id: Mapped[UUID]
    report_version_id: Mapped[UUID]
    original_version_id: Mapped[UUID]
    generation_id: Mapped[UUID]
    actor_membership_id: Mapped[UUID]
    decision_id: Mapped[UUID | None]
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(24))
    decision: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(2000))
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReportVerificationModel(Base):
    __tablename__ = "report_version_verifications"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "verification_job_id"),
        ForeignKeyConstraint(
            ["organization_id", "report_id", "report_version_id"],
            ["report_versions.organization_id", "report_versions.report_id", "report_versions.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "verification_job_id"],
            ["report_generation_jobs.organization_id", "report_generation_jobs.id"],
        ),
        Index(
            "ix_report_verifications_version",
            "organization_id",
            "report_id",
            "report_version_id",
            "created_at",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    report_id: Mapped[UUID]
    report_version_id: Mapped[UUID]
    verification_job_id: Mapped[UUID]
    verdict: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FeedbackOutcomeModel(Base):
    __tablename__ = "feedback_outcomes"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint(
            "organization_id",
            "feedback_id",
            "source_type",
            "source_id",
            "source_version",
            name="uq_feedback_outcome_source",
        ),
        ForeignKeyConstraint(
            ["organization_id", "feedback_id"], ["feedback.organization_id", "feedback.id"]
        ),
        ForeignKeyConstraint(
            ["organization_id", "actor_membership_id"],
            ["memberships.organization_id", "memberships.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "task_transition_id"],
            ["task_status_transitions.organization_id", "task_status_transitions.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "actual_task_id"], ["tasks.organization_id", "tasks.id"]
        ),
        ForeignKeyConstraint(
            ["organization_id", "observation_id"],
            ["task_progress_observations.organization_id", "task_progress_observations.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "blocker_transition_id"],
            ["blocker_transitions.organization_id", "blocker_transitions.id"],
        ),
        CheckConstraint(
            "state IN ('AVAILABLE','UNKNOWN') AND schema_version='feedback-outcome.v1' A"
            "ND source_version>=0",
            name="state",
        ),
        CheckConstraint(
            "(source_type='TASK_TRANSITION' AND task_transition_id IS NOT NULL AND sourc"
            "e_id=task_transition_id AND actual_task_id IS NULL AND blocker_transition_i"
            "d IS NULL AND observation_id IS NULL) OR (source_type='TASK_ACTUALS' AND ac"
            "tual_task_id IS NOT NULL AND source_id=actual_task_id AND task_transition_i"
            "d IS NULL AND blocker_transition_id IS NULL) OR (source_type='BLOCKER_RESOL"
            "UTION' AND blocker_transition_id IS NOT NULL AND source_id=blocker_transiti"
            "on_id AND actual_task_id IS NULL AND task_transition_id IS NULL AND observa"
            "tion_id IS NULL)",
            name="source",
        ),
        Index(
            "ix_feedback_outcomes_feedback", "organization_id", "feedback_id", "recorded_at", "id"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    feedback_id: Mapped[UUID]
    actor_membership_id: Mapped[UUID]
    schema_version: Mapped[str] = mapped_column(String(32))
    source_type: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[UUID]
    source_version: Mapped[int]
    task_transition_id: Mapped[UUID | None]
    actual_task_id: Mapped[UUID | None]
    observation_id: Mapped[UUID | None]
    blocker_transition_id: Mapped[UUID | None]
    state: Mapped[str] = mapped_column(String(16))
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
