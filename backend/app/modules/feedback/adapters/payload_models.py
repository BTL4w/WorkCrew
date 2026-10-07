"""Retention provenance only: no duplicated context/prompt store."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class RetentionPayloadModel(Base):
    __tablename__ = "ai_retention_payloads"
    __table_args__ = (
        ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        CheckConstraint(
            (
                "classification IN ('RAW_CONTEXT','REDACTED_TRACE') AND fence>=0 AND "
                "expires_at>=created_at AND expires_at<=created_at + CASE WHEN "
                "classification='RAW_CONTEXT' THEN interval '30 days' ELSE interval '90 "
                "days' END"
            ),
            name="retention",
        ),
        Index(
            "ix_ai_retention_payloads_expired",
            "organization_id",
            "expires_at",
            "storage_kind",
            "owner_id",
            postgresql_where=text("purged_at IS NULL"),
        ),
    )
    organization_id: Mapped[UUID] = mapped_column(primary_key=True)
    storage_kind: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(primary_key=True)
    classification: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    run_id: Mapped[UUID | None]
    fence: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
