"""Tenant/FK-bound immutable triggers/snapshots and durable delivery jobs."""

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


class SummaryTriggerModel(Base):
    __tablename__ = "daily_summary_triggers"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "schedule_id", "window_id"),
        ForeignKeyConstraint(
            ["organization_id", "schedule_id"],
            ["automation_schedules.organization_id", "automation_schedules.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "window_id"],
            ["reporting_windows.organization_id", "reporting_windows.id"],
        ),
        Index("ix_summary_trigger_window", "organization_id", "window_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    schedule_id: Mapped[UUID]
    window_id: Mapped[UUID]
    applied_version: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SummarySnapshotModel(Base):
    __tablename__ = "daily_summary_snapshots"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "trigger_id"),
        ForeignKeyConstraint(
            ["organization_id", "trigger_id"],
            ["daily_summary_triggers.organization_id", "daily_summary_triggers.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "project_id"], ["projects.organization_id", "projects.id"]
        ),
        Index("ix_summary_snapshot_project", "organization_id", "project_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    trigger_id: Mapped[UUID]
    project_id: Mapped[UUID]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SummaryDeliveryModel(Base):
    __tablename__ = "daily_summary_deliveries"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "snapshot_id", "recipient_id"),
        ForeignKeyConstraint(
            ["organization_id", "snapshot_id"],
            ["daily_summary_snapshots.organization_id", "daily_summary_snapshots.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "recipient_id"], ["memberships.organization_id", "memberships.id"]
        ),
        CheckConstraint("attempts BETWEEN 0 AND 3", name="attempts"),
        CheckConstraint(
            "state IN ('PENDING','RUNNING','DELIVERED','FAILED','CANCELLED')", name="state"
        ),
        Index("ix_summary_delivery_claim", "organization_id", "state", "retry_at", "lease_until"),
        Index("ix_summary_delivery_feed", "organization_id", "recipient_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    snapshot_id: Mapped[UUID]
    recipient_id: Mapped[UUID]
    state: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int]
    worker_id: Mapped[str | None] = mapped_column(String(160))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
