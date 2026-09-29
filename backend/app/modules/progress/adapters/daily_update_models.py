"""Tenant-owned drafts and immutable reporting facts."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DailyUpdateDraftModel(Base):
    __tablename__ = "daily_update_drafts"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "owner_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("version > 0", name="version_positive"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    owner_membership_id: Mapped[UUID]
    version: Mapped[int]
    confirmed_update_id: Mapped[UUID | None]


class DailyUpdateDraftRevisionModel(Base):
    __tablename__ = "daily_update_draft_revisions"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "draft_id"],
            ["daily_update_drafts.organization_id", "daily_update_drafts.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "owner_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "draft_id", "version"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    draft_id: Mapped[UUID]
    version: Mapped[int]
    owner_membership_id: Mapped[UUID]
    content_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class DailyUpdateModel(Base):
    __tablename__ = "daily_updates"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "draft_id"],
            ["daily_update_drafts.organization_id", "daily_update_drafts.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "owner_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "draft_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    draft_id: Mapped[UUID]
    owner_membership_id: Mapped[UUID]
    draft_version: Mapped[int]
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TaskProgressObservationModel(Base):
    __tablename__ = "task_progress_observations"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "update_id"],
            ["daily_updates.organization_id", "daily_updates.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "owner_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "corrects_observation_id"],
            ["task_progress_observations.organization_id", "task_progress_observations.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "task_id", "progress_version"),
        UniqueConstraint("organization_id", "corrects_observation_id"),
        CheckConstraint("reported_percent BETWEEN 0 AND 100", name="reported_percent_range"),
        CheckConstraint("remaining_hours BETWEEN 0 AND 10000", name="remaining_hours_range"),
        CheckConstraint("progress_version > 0", name="version_positive"),
        Index(
            "ix_task_progress_observations_task",
            "organization_id",
            "task_id",
            "reporting_at",
            "progress_version",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    update_id: Mapped[UUID]
    task_id: Mapped[UUID]
    owner_membership_id: Mapped[UUID]
    progress_version: Mapped[int]
    reporting_date: Mapped[date] = mapped_column(Date)
    reporting_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reported_percent: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    remaining_hours: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    corrects_observation_id: Mapped[UUID | None]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class TaskActualProjectionModel(Base):
    __tablename__ = "task_actual_projections"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "observation_id"],
            ["task_progress_observations.organization_id", "task_progress_observations.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "task_id"),
        CheckConstraint("progress_version >= 0", name="version_nonnegative"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    task_id: Mapped[UUID]
    progress_version: Mapped[int]
    observation_id: Mapped[UUID | None]


class WorkLogModel(Base):
    __tablename__ = "work_logs"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "observation_id"],
            ["task_progress_observations.organization_id", "task_progress_observations.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "owner_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "supersedes_log_id"],
            ["work_logs.organization_id", "work_logs.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "observation_id"),
        UniqueConstraint("organization_id", "supersedes_log_id"),
        CheckConstraint("spent_hours BETWEEN 0 AND 24", name="spent_hours_range"),
        Index(
            "ix_work_logs_owner_date", "organization_id", "owner_membership_id", "reporting_date"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    observation_id: Mapped[UUID]
    owner_membership_id: Mapped[UUID]
    reporting_date: Mapped[date] = mapped_column(Date)
    spent_hours: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    supersedes_log_id: Mapped[UUID | None]


class DailyUpdateEvidenceLinkModel(Base):
    __tablename__ = "daily_update_evidence_links"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "observation_id"],
            ["task_progress_observations.organization_id", "task_progress_observations.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "evidence_id", "evidence_version"],
            [
                "evidence_originals.organization_id",
                "evidence_originals.id",
                "evidence_originals.version",
            ],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "observation_id", "evidence_id", "evidence_version"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    observation_id: Mapped[UUID]
    evidence_id: Mapped[UUID]
    evidence_version: Mapped[int]


class WeeklyActualSnapshotModel(Base):
    __tablename__ = "weekly_actual_snapshots"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "project_week_id"],
            ["project_weeks.organization_id", "project_weeks.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "owner_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "update_id"],
            ["daily_updates.organization_id", "daily_updates.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("kind IN ('CURRENT', 'LATE', 'FINAL')", name="kind"),
        Index(
            "ix_weekly_actual_snapshots_week", "organization_id", "project_week_id", "captured_at"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    project_week_id: Mapped[UUID]
    owner_membership_id: Mapped[UUID]
    update_id: Mapped[UUID | None]
    kind: Mapped[str] = mapped_column(String(16))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
