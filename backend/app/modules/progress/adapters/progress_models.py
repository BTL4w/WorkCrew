"""Append-only tenant-owned plan snapshots; JSON preserves deleted graph references."""

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


class WeeklyPlanBaselineModel(Base):
    __tablename__ = "weekly_plan_baselines"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "project_week_id"],
            ["project_weeks.organization_id", "project_weeks.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "project_week_id", "sequence"),
        CheckConstraint("sequence > 0", name="sequence_positive"),
        CheckConstraint("kind IN ('ENTRY', 'MANUAL', 'APPROVED')", name="kind"),
        Index("ix_weekly_plan_baselines_week", "organization_id", "project_week_id", "sequence"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    project_week_id: Mapped[UUID]
    sequence: Mapped[int]
    kind: Mapped[str] = mapped_column(String(16))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
