"""Tenant-keyed schedules, immutable configuration versions, drafts and window rosters."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKeyConstraint, Index, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ScheduleModel(Base):
    __tablename__ = "automation_schedules"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "project_id"),
        ForeignKeyConstraint(
            ["organization_id", "project_id"], ["projects.organization_id", "projects.id"]
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    project_id: Mapped[UUID]
    version: Mapped[int] = mapped_column(Integer)
    paused: Mapped[bool] = mapped_column(Boolean)


class ScheduleVersionModel(Base):
    __tablename__ = "automation_schedule_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "schedule_id"],
            ["automation_schedules.organization_id", "automation_schedules.id"],
        ),
        Index(
            "ix_schedule_version_effective",
            "organization_id",
            "schedule_id",
            "effective_at",
            "version",
        ),
    )
    organization_id: Mapped[UUID] = mapped_column(primary_key=True)
    schedule_id: Mapped[UUID] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ScheduleRecipientModel(Base):
    __tablename__ = "automation_schedule_recipients"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "schedule_id", "version"],
            [
                "automation_schedule_versions.organization_id",
                "automation_schedule_versions.schedule_id",
                "automation_schedule_versions.version",
            ],
        ),
        ForeignKeyConstraint(
            ["organization_id", "membership_id"], ["memberships.organization_id", "memberships.id"]
        ),
    )
    organization_id: Mapped[UUID] = mapped_column(primary_key=True)
    schedule_id: Mapped[UUID] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    membership_id: Mapped[UUID] = mapped_column(primary_key=True)


class ScheduleDraftModel(Base):
    __tablename__ = "automation_schedule_drafts"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "project_id"], ["projects.organization_id", "projects.id"]
        ),
        ForeignKeyConstraint(
            ["organization_id", "owner_membership_id"],
            ["memberships.organization_id", "memberships.id"],
        ),
        Index("ix_schedule_draft_expiry", "organization_id", "expires_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    project_id: Mapped[UUID]
    owner_membership_id: Mapped[UUID]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ReportingWindowModel(Base):
    __tablename__ = "reporting_windows"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "schedule_id", "starts_at"),
        ForeignKeyConstraint(
            ["organization_id", "schedule_id", "applied_version"],
            [
                "automation_schedule_versions.organization_id",
                "automation_schedule_versions.schedule_id",
                "automation_schedule_versions.version",
            ],
        ),
        Index(
            "ix_reporting_window_lookup", "organization_id", "schedule_id", "starts_at", "ends_at"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    schedule_id: Mapped[UUID]
    applied_version: Mapped[int]
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class WindowReporterModel(Base):
    __tablename__ = "reporting_window_reporters"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "window_id"],
            ["reporting_windows.organization_id", "reporting_windows.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "membership_id"], ["memberships.organization_id", "memberships.id"]
        ),
    )
    organization_id: Mapped[UUID] = mapped_column(primary_key=True)
    window_id: Mapped[UUID] = mapped_column(primary_key=True)
    membership_id: Mapped[UUID] = mapped_column(primary_key=True)


class TaskScopeHistoryModel(Base):
    """Assignment/status facts needed to reconstruct a roster after a delayed opening."""

    __tablename__ = "automation_task_scope_history"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(["organization_id", "task_id"], ["tasks.organization_id", "tasks.id"]),
        ForeignKeyConstraint(
            ["organization_id", "assignee_membership_id"],
            ["memberships.organization_id", "memberships.id"],
        ),
        Index("ix_task_scope_history_at", "organization_id", "task_id", "recorded_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    task_id: Mapped[UUID]
    assignee_membership_id: Mapped[UUID | None]
    status: Mapped[str]
    reporter_active: Mapped[bool] = mapped_column(Boolean)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
