"""Tenant-qualified leased generation and whole-run usage metadata."""

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


class ReportGenerationJobModel(Base):
    __tablename__ = "report_generation_jobs"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "requester_membership_id", "request_key"),
        ForeignKeyConstraint(
            ["organization_id", "report_id", "base_version_id", "snapshot_id"],
            [
                "report_versions.organization_id",
                "report_versions.report_id",
                "report_versions.id",
                "report_versions.snapshot_id",
            ],
        ),
        ForeignKeyConstraint(
            ["organization_id", "report_id", "snapshot_id", "snapshot_hash"],
            [
                "report_metric_snapshots.organization_id",
                "report_metric_snapshots.report_id",
                "report_metric_snapshots.id",
                "report_metric_snapshots.snapshot_hash",
            ],
        ),
        ForeignKeyConstraint(
            ["organization_id", "requester_membership_id"],
            ["memberships.organization_id", "memberships.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "orchestration_run_id"],
            ["orchestration_runs.organization_id", "orchestration_runs.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "report_id", "proposed_version_id"],
            ["report_versions.organization_id", "report_versions.report_id", "report_versions.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "original_generation_id"],
            ["report_generation_jobs.organization_id", "report_generation_jobs.id"],
        ),
        CheckConstraint(
            "job_type IN ('DRAFT','EDIT_VERIFICATION') AND "
            "((job_type='EDIT_VERIFICATION') = (original_generation_id IS NOT NULL))",
            name="job_type",
        ),
        CheckConstraint(
            "state IN ('QUEUED','RUNNING','AWAITING_REVIEW','AI_UNAVAILABLE','FAILED')",
            name="state",
        ),
        CheckConstraint(
            "claims BETWEEN 0 AND 3 AND fence>=0 AND expected_report_version>=1", name="bounds"
        ),
        CheckConstraint(
            "(state='RUNNING') = (lease_owner IS NOT NULL AND lease_until IS NOT NULL)",
            name="lease",
        ),
        CheckConstraint("(started_at IS NULL) = (deadline IS NULL)", name="deadline"),
        Index("ix_report_generation_claim", "organization_id", "state", "created_at", "id"),
        Index("ix_report_generation_report", "organization_id", "report_id", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    report_id: Mapped[UUID]
    base_version_id: Mapped[UUID]
    snapshot_id: Mapped[UUID]
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    requester_membership_id: Mapped[UUID]
    request_key: Mapped[str] = mapped_column(String(128))
    expected_report_version: Mapped[int]
    state: Mapped[str] = mapped_column(String(24))
    claims: Mapped[int] = mapped_column(default=0, server_default="0")
    fence: Mapped[int] = mapped_column(default=0, server_default="0")
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    orchestration_run_id: Mapped[UUID | None]
    proposed_version_id: Mapped[UUID | None]
    job_type: Mapped[str] = mapped_column(String(24), server_default="DRAFT", default="DRAFT")
    original_generation_id: Mapped[UUID | None]
    safe_error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReportGenerationUsageModel(Base):
    __tablename__ = "report_generation_usage"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "generation_id"],
            ["report_generation_jobs.organization_id", "report_generation_jobs.id"],
        ),
        CheckConstraint(
            "attempts BETWEEN 0 AND 3 AND input_reserved BETWEEN 0 AND 48000 AND "
            "output_reserved BETWEEN 0 AND 8000",
            name="model_bounds",
        ),
        CheckConstraint("tools BETWEEN 0 AND 6 AND retries BETWEEN 0 AND 1", name="tool_bounds"),
    )
    organization_id: Mapped[UUID] = mapped_column(primary_key=True)
    generation_id: Mapped[UUID] = mapped_column(primary_key=True)
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    input_reserved: Mapped[int] = mapped_column(default=0, server_default="0")
    output_reserved: Mapped[int] = mapped_column(default=0, server_default="0")
    tools: Mapped[int] = mapped_column(default=0, server_default="0")
    retries: Mapped[int] = mapped_column(default=0, server_default="0")
    reservations: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
