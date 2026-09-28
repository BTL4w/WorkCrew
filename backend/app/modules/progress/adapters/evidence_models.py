"""Tenant-owned upload reservations and immutable original metadata."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class EvidenceOriginalModel(Base):
    __tablename__ = "evidence_originals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "uploader_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "id", "version"),
        UniqueConstraint("organization_id", "uploader_membership_id", "idempotency_key"),
        UniqueConstraint("organization_id", "storage_key"),
        CheckConstraint("state IN ('RESERVED', 'READY', 'EXPIRED')", name="state"),
        CheckConstraint("version = 1", name="version"),
        CheckConstraint(
            "state != 'READY' OR (sha256 IS NOT NULL AND byte_length > 0 "
            "AND mime_type IS NOT NULL)",
            name="ready_metadata",
        ),
        Index("ix_evidence_originals_owner", "organization_id", "uploader_membership_id", "id"),
        Index("ix_evidence_originals_expiry", "organization_id", "expires_at", "lease_until"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    uploader_membership_id: Mapped[UUID]
    version: Mapped[int]
    filename: Mapped[str] = mapped_column(String(255))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    storage_key: Mapped[str] = mapped_column(String(160))
    state: Mapped[str] = mapped_column(String(16))
    sha256: Mapped[str | None] = mapped_column(String(64))
    byte_length: Mapped[int | None]
    mime_type: Mapped[str | None] = mapped_column(String(100))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_id: Mapped[UUID]
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
