"""Append-only Task completion attestations."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TaskCompletionCheckModel(Base):
    __tablename__ = "task_completion_checks"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "transition_id"],
            ["task_status_transitions.organization_id", "task_status_transitions.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "actor_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "observation_id"],
            ["task_progress_observations.organization_id", "task_progress_observations.id"],
            name="fk_task_completion_checks_observation_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "transition_id", "criterion_id"),
        Index("ix_task_completion_checks_task", "organization_id", "task_id", "occurred_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    task_id: Mapped[UUID]
    transition_id: Mapped[UUID]
    actor_membership_id: Mapped[UUID]
    task_version_after: Mapped[int]
    criterion_id: Mapped[UUID | None]
    criterion_version: Mapped[int | None]
    confirmed: Mapped[bool] = mapped_column(Boolean)
    observation_id: Mapped[UUID | None]
    evidence_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
