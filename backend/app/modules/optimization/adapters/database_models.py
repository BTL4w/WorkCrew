"""Durable cache invalidation metadata, owned by the source transaction."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ReadModelGeneration(Base):
    __tablename__ = "read_model_generations"
    __table_args__ = (CheckConstraint("generation >= 0", name="nonnegative_generation"),)

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    epoch: Mapped[UUID] = mapped_column(server_default=text("gen_random_uuid()"))
    generation: Mapped[int] = mapped_column(BigInteger, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
