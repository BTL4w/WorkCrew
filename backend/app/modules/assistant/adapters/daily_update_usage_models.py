"""Usage metadata only: no report text or original media is stored here."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class UsageColumns:
    organization_id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_membership_id: Mapped[UUID] = mapped_column(primary_key=True)
    resource_id: Mapped[UUID] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    attempts: Mapped[int]
    input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentUsageBudgetModel(UsageColumns, Base):
    __tablename__ = "agent_usage_budgets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "owner_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("attempts BETWEEN 0 AND 3", name="attempts"),
        CheckConstraint("input_tokens BETWEEN 0 AND 24000", name="inputs"),
        CheckConstraint("output_tokens BETWEEN 0 AND 4000", name="outputs"),
        CheckConstraint("version = 0", name="version"),
    )


class EvidenceModelBudgetModel(UsageColumns, Base):
    __tablename__ = "evidence_model_budgets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "owner_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "resource_id", "version"],
            [
                "evidence_originals.organization_id",
                "evidence_originals.id",
                "evidence_originals.version",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint("attempts BETWEEN 0 AND 10", name="attempts"),
        CheckConstraint("input_tokens BETWEEN 0 AND 240000", name="inputs"),
        CheckConstraint("output_tokens BETWEEN 0 AND 40000", name="outputs"),
        CheckConstraint("version > 0", name="version"),
    )
