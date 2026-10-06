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
