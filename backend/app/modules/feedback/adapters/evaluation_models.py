"""Tenant-qualified candidates, immutable reviewed revisions and frozen manifests."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class EvaluationCandidateModel(Base):
    __tablename__ = "evaluation_candidates"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "feedback_id", name="uq_evaluation_candidate_feedback"),
        ForeignKeyConstraint(
            ["organization_id", "feedback_id"], ["feedback.organization_id", "feedback.id"]
        ),
        CheckConstraint(
            "version>=1 AND status IN ('PENDING_REVIEW','CURATED','DUPLICATE')", name="state"
        ),
        CheckConstraint(
            "payload_classification='RAW_CANDIDATE' AND expires_at>created_at", name="retention"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    feedback_id: Mapped[UUID]
    version: Mapped[int]
    status: Mapped[str] = mapped_column(String(24))
    payload_classification: Mapped[str] = mapped_column(String(24))
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    source_outcome_ids: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvaluationCaseModel(Base):
    __tablename__ = "evaluation_cases"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint(
            "organization_id", "candidate_id", "locale", name="uq_evaluation_case_candidate_locale"
        ),
        ForeignKeyConstraint(
            ["organization_id", "candidate_id"],
            ["evaluation_candidates.organization_id", "evaluation_candidates.id"],
        ),
        CheckConstraint(
            "current_version>=1 AND split IN ('GOLDEN','HELD_OUT') AND locale IN ('vi','en')",
            name="state",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    candidate_id: Mapped[UUID]
    locale: Mapped[str] = mapped_column(String(2))
    split: Mapped[str] = mapped_column(String(16))
    current_version: Mapped[int]


class EvaluationCaseRevisionModel(Base):
    __tablename__ = "evaluation_case_revisions"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "case_id", "version", "case_hash"),
        UniqueConstraint("organization_id", "case_id", "version"),
        UniqueConstraint("organization_id", "case_hash", name="uq_evaluation_case_hash"),
        ForeignKeyConstraint(
            ["organization_id", "case_id"],
            ["evaluation_cases.organization_id", "evaluation_cases.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "curator_membership_id"],
            ["memberships.organization_id", "memberships.id"],
        ),
        CheckConstraint(
            "version>=1 AND origin IN ('SYNTHETIC','REDACTED') AND split IN ('GOLDEN','HELD_OUT')",
            name="state",
        ),
        CheckConstraint("policy_version='report-eval-dataset.v1'", name="policy"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    case_id: Mapped[UUID]
    version: Mapped[int]
    case_hash: Mapped[str] = mapped_column(String(64))
    origin: Mapped[str] = mapped_column(String(16))
    split: Mapped[str] = mapped_column(String(16))
    policy_version: Mapped[str] = mapped_column(String(32))
    curator_membership_id: Mapped[UUID]
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvaluationDatasetVersionModel(Base):
    __tablename__ = "evaluation_dataset_versions"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "name", "split", "version"),
        UniqueConstraint(
            "organization_id", "name", "split", "dataset_hash", name="uq_evaluation_dataset_hash"
        ),
        ForeignKeyConstraint(
            ["organization_id", "frozen_by_membership_id"],
            ["memberships.organization_id", "memberships.id"],
        ),
        CheckConstraint(
            "version>=1 AND split IN ('GOLDEN','HELD_OUT') AND "
            "policy_version='report-eval-dataset.v1'",
            name="state",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    name: Mapped[str] = mapped_column(String(64))
    split: Mapped[str] = mapped_column(String(16))
    version: Mapped[int]
    policy_version: Mapped[str] = mapped_column(String(32))
    dataset_hash: Mapped[str] = mapped_column(String(64))
    frozen_by_membership_id: Mapped[UUID]
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvaluationDatasetCaseModel(Base):
    __tablename__ = "evaluation_dataset_cases"
    __table_args__ = (
        UniqueConstraint("organization_id", "dataset_id", "case_id", "case_version"),
        UniqueConstraint("organization_id", "dataset_id", "case_hash"),
        ForeignKeyConstraint(
            ["organization_id", "dataset_id"],
            ["evaluation_dataset_versions.organization_id", "evaluation_dataset_versions.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "case_id", "case_version", "case_hash"],
            [
                "evaluation_case_revisions.organization_id",
                "evaluation_case_revisions.case_id",
                "evaluation_case_revisions.version",
                "evaluation_case_revisions.case_hash",
            ],
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    dataset_id: Mapped[UUID]
    case_id: Mapped[UUID]
    case_version: Mapped[int]
    case_hash: Mapped[str] = mapped_column(String(64))
