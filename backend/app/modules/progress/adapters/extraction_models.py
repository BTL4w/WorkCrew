"""Retired 0022 schema metadata retained for migrations; no runtime extraction."""

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


class EvidenceProcessingJobModel(Base):
    __tablename__ = "evidence_processing_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "evidence_id", "version"],
            [
                "evidence_originals.organization_id",
                "evidence_originals.id",
                "evidence_originals.version",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "uploader_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "evidence_id", "version", "revision"),
        UniqueConstraint("organization_id", "uploader_membership_id", "idempotency_key"),
        CheckConstraint(
            "state IN ('PENDING','PROCESSING','READY','PARTIAL','UNREADABLE','FAILED')",
            name="state",
        ),
        CheckConstraint(
            "model_attempts BETWEEN 0 AND 10 AND lease_attempts BETWEEN 0 AND 3", name="budgets"
        ),
        Index("ix_evidence_processing_jobs_ready", "organization_id", "state", "lease_until"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    evidence_id: Mapped[UUID]
    version: Mapped[int]
    uploader_membership_id: Mapped[UUID]
    revision: Mapped[int]
    idempotency_key: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(16))
    lease_id: Mapped[UUID | None]
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_attempts: Mapped[int]
    model_attempts: Mapped[int]
    input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    next_visual: Mapped[int]
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvidenceSegmentModel(Base):
    __tablename__ = "evidence_segments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["evidence_processing_jobs.organization_id", "evidence_processing_jobs.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "evidence_id", "version"],
            [
                "evidence_originals.organization_id",
                "evidence_originals.id",
                "evidence_originals.version",
            ],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "job_id", "source_id"),
        Index("ix_evidence_segments_source", "organization_id", "evidence_id", "version"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    job_id: Mapped[UUID]
    evidence_id: Mapped[UUID]
    version: Mapped[int]
    source_id: Mapped[UUID]
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
