"""Tenant-scoped durable assessment jobs and append-only decisions."""

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


class TenantRow(Base):
    __abstract__ = True
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]


def task_fk():
    return ForeignKeyConstraint(
        ["organization_id", "task_id"], ["tasks.organization_id", "tasks.id"], ondelete="RESTRICT"
    )


def member_fk(column: str):
    return ForeignKeyConstraint(
        ["organization_id", column],
        ["memberships.organization_id", "memberships.id"],
        ondelete="RESTRICT",
    )


class RiskAssessmentModel(TenantRow):
    __tablename__ = "risk_assessments"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        task_fk(),
        UniqueConstraint("organization_id", "task_id", "cause_id"),
        Index("ix_risk_assessments_task", "organization_id", "task_id", "created_at", "id"),
    )
    task_id: Mapped[UUID]
    cause_id: Mapped[UUID]
    input_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RiskJobModel(TenantRow):
    __tablename__ = "risk_refresh_jobs"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        task_fk(),
        member_fk("actor_membership_id"),
        UniqueConstraint("organization_id", "task_id", "cause_id"),
        CheckConstraint("attempts BETWEEN 0 AND 3", name="attempts"),
        CheckConstraint("state IN ('PENDING','RUNNING','DONE','FAILED')", name="state"),
        Index("ix_risk_jobs_claim", "organization_id", "state", "lease_until", "created_at"),
    )
    task_id: Mapped[UUID]
    actor_membership_id: Mapped[UUID]
    cause_id: Mapped[UUID]
    input_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RiskReviewModel(TenantRow):
    __tablename__ = "risk_review_events"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "risk_id"],
            ["risk_assessments.organization_id", "risk_assessments.id"],
            ondelete="RESTRICT",
        ),
        member_fk("actor_membership_id"),
        Index("ix_risk_reviews", "organization_id", "risk_id", "created_at", "id"),
    )
    risk_id: Mapped[UUID]
    actor_membership_id: Mapped[UUID]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RiskNotificationModel(TenantRow):
    __tablename__ = "risk_notifications"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        task_fk(),
        member_fk("recipient_membership_id"),
        ForeignKeyConstraint(
            ["organization_id", "risk_id"],
            ["risk_assessments.organization_id", "risk_assessments.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "recipient_membership_id", "dedup_key"),
        Index(
            "ix_risk_notifications_feed",
            "organization_id",
            "recipient_membership_id",
            "created_at",
            "id",
        ),
    )
    task_id: Mapped[UUID]
    risk_id: Mapped[UUID]
    recipient_membership_id: Mapped[UUID]
    dedup_key: Mapped[str] = mapped_column(String(160))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    read: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
