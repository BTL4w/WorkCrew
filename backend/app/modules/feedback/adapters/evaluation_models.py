"""Tenant-qualified candidates, immutable reviewed revisions and frozen manifests."""

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


class EvaluationRunModel(Base):
    __tablename__ = "evaluation_runs"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "id", "dataset_version_id"),
        Index("ix_evaluation_runs_queue", "organization_id", "status", "lease_until", "created_at"),
        UniqueConstraint("organization_id", "requester_membership_id", "request_key"),
        ForeignKeyConstraint(
            ["organization_id", "dataset_version_id"],
            ["evaluation_dataset_versions.organization_id", "evaluation_dataset_versions.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "requester_membership_id"],
            ["memberships.organization_id", "memberships.id"],
        ),
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','PASSED','FAILED','CANCELLED') AND provider "
            "IN ('mock','hosted') AND fence>=0 AND attempts>=0 AND attempts<=3 AND "
            "budget_tokens BETWEEN 1 AND 1000000",
            name="state",
        ),
        CheckConstraint("provider_policy_version='report-eval-provider.v1'", name="policy"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    requester_membership_id: Mapped[UUID]
    request_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    dataset_version_id: Mapped[UUID]
    dataset_version: Mapped[int]
    dataset_hash: Mapped[str] = mapped_column(String(64))
    dataset_policy_version: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(16))
    provider_policy_version: Mapped[str] = mapped_column(String(32))
    provider_config_hash: Mapped[str] = mapped_column(String(64))
    budget_tokens: Mapped[int]
    status: Mapped[str] = mapped_column(String(16))
    failure_kind: Mapped[str | None] = mapped_column(String(16))
    safe_error_code: Mapped[str | None] = mapped_column(String(64))
    fence: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    attempts: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class EvaluationResultModel(Base):
    __tablename__ = "evaluation_results"
    __table_args__ = (
        UniqueConstraint("organization_id", "run_id", "case_id", "case_version"),
        ForeignKeyConstraint(
            ["organization_id", "run_id", "dataset_version_id"],
            [
                "evaluation_runs.organization_id",
                "evaluation_runs.id",
                "evaluation_runs.dataset_version_id",
            ],
        ),
        ForeignKeyConstraint(
            ["organization_id", "dataset_version_id", "case_id", "case_version"],
            [
                "evaluation_dataset_cases.organization_id",
                "evaluation_dataset_cases.dataset_id",
                "evaluation_dataset_cases.case_id",
                "evaluation_dataset_cases.case_version",
            ],
        ),
        CheckConstraint("case_version>=1", name="version"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    run_id: Mapped[UUID]
    dataset_version_id: Mapped[UUID]
    case_id: Mapped[UUID]
    case_version: Mapped[int]
    case_hash: Mapped[str] = mapped_column(String(64))
    measurement: Mapped[dict[str, Any]] = mapped_column(JSONB)
