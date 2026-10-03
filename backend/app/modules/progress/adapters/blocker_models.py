"""Tenant-owned current blockers and append-only lifecycle/source links."""

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


class BlockerModel(Base):
    __tablename__ = "blockers"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "created_by_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("status IN ('OPEN','ACKNOWLEDGED','RESOLVED')", name="status"),
        CheckConstraint("severity IN ('LOW','MEDIUM','HIGH','CRITICAL')", name="severity"),
        CheckConstraint("version > 0", name="version"),
        CheckConstraint("NOT archived OR status='RESOLVED'", name="archive_resolved"),
        Index("ix_blockers_task", "organization_id", "task_id", "status", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    task_id: Mapped[UUID]
    created_by_membership_id: Mapped[UUID]
    version: Mapped[int]
    severity: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    archived: Mapped[bool]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BlockerTransitionModel(Base):
    __tablename__ = "blocker_transitions"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "blocker_id", "version"),
        ForeignKeyConstraint(
            ["organization_id", "blocker_id"],
            ["blockers.organization_id", "blockers.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "actor_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "update_id"],
            ["daily_updates.organization_id", "daily_updates.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("version > 0", name="version"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    blocker_id: Mapped[UUID]
    version: Mapped[int]
    actor_membership_id: Mapped[UUID]
    update_id: Mapped[UUID | None]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BlockerEvidenceModel(Base):
    __tablename__ = "blocker_evidence_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "blocker_id", "blocker_version"],
            [
                "blocker_transitions.organization_id",
                "blocker_transitions.blocker_id",
                "blocker_transitions.version",
            ],
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
    )
    organization_id: Mapped[UUID] = mapped_column(primary_key=True)
    blocker_id: Mapped[UUID] = mapped_column(primary_key=True)
    blocker_version: Mapped[int] = mapped_column(primary_key=True)
    evidence_id: Mapped[UUID] = mapped_column(primary_key=True)
    evidence_version: Mapped[int]
