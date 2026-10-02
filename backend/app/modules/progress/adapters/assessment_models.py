"""Tenant-owned bounded assessment attempts and immutable confirmation snapshots."""

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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class EvidenceAssessmentModel(Base):
    __tablename__ = "evidence_assessments"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "draft_id", "draft_version", "attempt"),
        ForeignKeyConstraint(
            ["organization_id", "draft_id", "draft_version"],
            [
                "daily_update_draft_revisions.organization_id",
                "daily_update_draft_revisions.draft_id",
                "daily_update_draft_revisions.version",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "owner_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "state IN ('PENDING','READY','UNAVAILABLE','NOT_ASSESSED_NO_EVIDENCE')", name="state"
        ),
        CheckConstraint("attempt > 0", name="attempt_positive"),
        Index(
            "ix_evidence_assessments_draft",
            "organization_id",
            "draft_id",
            "draft_version",
            "attempt",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    draft_id: Mapped[UUID]
    draft_version: Mapped[int]
    attempt: Mapped[int]
    owner_membership_id: Mapped[UUID]
    content_hash: Mapped[str] = mapped_column(String(64))
    binding: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WarningAcknowledgmentModel(Base):
    __tablename__ = "warning_acknowledgments"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "update_id"),
        ForeignKeyConstraint(
            ["organization_id", "update_id"],
            ["daily_updates.organization_id", "daily_updates.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "assessment_id"],
            ["evidence_assessments.organization_id", "evidence_assessments.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "owner_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    update_id: Mapped[UUID]
    assessment_id: Mapped[UUID]
    owner_membership_id: Mapped[UUID]
    acknowledged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
