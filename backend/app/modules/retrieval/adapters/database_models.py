"""Owner-scoped immutable evidence and monotonic paid-attempt accounting."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

import app.modules.assistant.adapters.database_models as _assistant_models
from app.core.database import Base

_DEPENDENCIES = (_assistant_models,)
_CAPS = {
    "queries": 2,
    "page_reads": 3,
    "provider_requests": 8,
    "tool_calls": 8,
    "nodes": 12,
    "model_attempts": 2,
    "input_tokens": 12000,
    "output_tokens": 3200,
}


class RetrievalRunBudgetModel(Base):
    __tablename__ = "retrieval_run_budgets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "agent_run_id"],
            ["agent_runs.organization_id", "agent_runs.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "orchestration_run_id"],
            ["orchestration_runs.organization_id", "orchestration_runs.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "owner_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["assistant_jobs.organization_id", "assistant_jobs.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "agent_run_id", "owner_membership_id"),
        CheckConstraint(
            "deadline>started_at AND deadline<=started_at+interval '25 seconds'", name="deadline"
        ),
        CheckConstraint(
            "expires_at>started_at AND expires_at<=started_at+interval '30 days'", name="expiry"
        ),
        CheckConstraint("version>=1 AND job_attempt>=1", name="versions"),
        CheckConstraint(
            "jsonb_typeof(caps)='object' AND jsonb_typeof(reserved)='object'",
            name="counter_objects",
        ),
        *(
            CheckConstraint(
                f"caps ? '{key}' AND reserved ? '{key}' "
                f"AND jsonb_typeof(caps->'{key}')='number' "
                f"AND jsonb_typeof(reserved->'{key}')='number' "
                f"AND (caps->>'{key}')::int BETWEEN 0 AND {cap} "
                f"AND (reserved->>'{key}')::int BETWEEN 0 AND (caps->>'{key}')::int",
                name=f"cap_{key}",
            )
            for key, cap in _CAPS.items()
        ),
    )
    organization_id: Mapped[UUID] = mapped_column(primary_key=True)
    agent_run_id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_membership_id: Mapped[UUID]
    orchestration_run_id: Mapped[UUID]
    job_id: Mapped[UUID]
    worker_id: Mapped[str] = mapped_column(String(255))
    job_attempt: Mapped[int]
    fencing_token: Mapped[UUID]
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB)
    caps: Mapped[dict[str, Any]] = mapped_column(JSONB)
    reserved: Mapped[dict[str, Any]] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, server_default="1")


class RetrievalAttemptModel(Base):
    __tablename__ = "retrieval_attempt_reservations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "agent_run_id", "owner_membership_id"],
            [
                "retrieval_run_budgets.organization_id",
                "retrieval_run_budgets.agent_run_id",
                "retrieval_run_budgets.owner_membership_id",
            ],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "agent_run_id", "operation_key", "attempt_no"),
        CheckConstraint("attempt_no BETWEEN 1 AND 2", name="attempt"),
        CheckConstraint("kind IN ('SEARCH','PAGE_READ','TOOL','NODE','MODEL')", name="kind"),
        CheckConstraint("status IN ('RESERVED','COMPLETED','UNKNOWN')", name="status"),
        CheckConstraint("request_hash ~ '^[a-f0-9]{64}$'", name="fingerprint"),
        CheckConstraint("cost_usd IS NULL OR cost_usd>=0", name="cost"),
    )
    organization_id: Mapped[UUID] = mapped_column(primary_key=True)
    id: Mapped[UUID] = mapped_column(primary_key=True)
    agent_run_id: Mapped[UUID]
    owner_membership_id: Mapped[UUID]
    operation_key: Mapped[str] = mapped_column(String(200))
    attempt_no: Mapped[int]
    kind: Mapped[str] = mapped_column(String(20))
    fencing_token: Mapped[UUID]
    request_hash: Mapped[str] = mapped_column(String(64))
    charged: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), server_default="RESERVED")
    dispatched: Mapped[bool] = mapped_column(Boolean, server_default="false")
    metadata_payload: Mapped[dict[str, Any]] = mapped_column(
        "result_metadata", JSONB, server_default=text("'{}'::jsonb")
    )
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 9))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RetrievalSourceSnapshotModel(Base):
    __tablename__ = "retrieval_source_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "agent_run_id", "owner_membership_id"],
            [
                "retrieval_run_budgets.organization_id",
                "retrieval_run_budgets.agent_run_id",
                "retrieval_run_budgets.owner_membership_id",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "agent_run_id", "tool_invocation_id"],
            [
                "tool_invocations.organization_id",
                "tool_invocations.agent_run_id",
                "tool_invocations.id",
            ],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "organization_id", "agent_run_id", "tool_invocation_id", "fingerprint", "canonical_url"
        ),
        CheckConstraint("fingerprint ~ '^[a-f0-9]{64}$'", name="fingerprint"),
        CheckConstraint(
            "expires_at>observed_at AND expires_at<=observed_at+interval '30 days'", name="expiry"
        ),
        CheckConstraint(
            "jsonb_typeof(payload)='object' AND octet_length(payload::text)<=300000",
            name="payload_bound",
        ),
    )
    organization_id: Mapped[UUID] = mapped_column(primary_key=True)
    id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_membership_id: Mapped[UUID]
    agent_run_id: Mapped[UUID]
    tool_invocation_id: Mapped[UUID]
    canonical_url: Mapped[str] = mapped_column(String(2048))
    fingerprint: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
