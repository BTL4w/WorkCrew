"""SQLAlchemy metadata for tenant-owned Assistant and Agent execution state."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

import app.modules.automations.adapters.delivery_models as _summary_models
import app.modules.organization.adapters.database_models as _organization_models
import app.modules.planning_runs.adapters.database_models as _planning_models
import app.modules.reporting.adapters.database_models as _reporting_models
from app.core.database import Base

_DEPENDENT_MODELS = (_organization_models, _planning_models, _reporting_models, _summary_models)


class AssistantConversationModel(Base):
    __tablename__ = "assistant_conversations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "owner_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "id"),
        CheckConstraint("locale IN ('vi', 'en')", name="locale"),
        CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name="status"),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint("last_message_sequence >= 0", name="message_sequence"),
        CheckConstraint("last_event_sequence >= 0", name="event_sequence"),
        Index(
            "ix_assistant_conversations_owner_timeline",
            "organization_id",
            "owner_membership_id",
            "updated_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    owner_membership_id: Mapped[UUID]
    locale: Mapped[str] = mapped_column(String(2))
    title: Mapped[str | None] = mapped_column(String(200))
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", server_default="ACTIVE")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    last_message_sequence: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_event_sequence: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AssistantMessageModel(Base):
    __tablename__ = "assistant_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["assistant_conversations.organization_id", "assistant_conversations.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "created_by_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "turn_id"],
            ["assistant_turns.organization_id", "assistant_turns.id"],
            name="fk_assistant_messages_turn",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "conversation_id", "sequence"),
        UniqueConstraint("organization_id", "conversation_id", "dedupe_key"),
        CheckConstraint("sequence >= 1", name="sequence"),
        CheckConstraint("role IN ('USER', 'ASSISTANT', 'SYSTEM')", name="role"),
        Index(
            "ix_assistant_messages_timeline",
            "organization_id",
            "conversation_id",
            "sequence",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    conversation_id: Mapped[UUID]
    sequence: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(20))
    content_blocks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    created_by_membership_id: Mapped[UUID | None]
    turn_id: Mapped[UUID | None]
    dedupe_key: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AssistantTurnModel(Base):
    __tablename__ = "assistant_turns"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["assistant_conversations.organization_id", "assistant_conversations.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "user_message_id"],
            ["assistant_messages.organization_id", "assistant_messages.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "actor_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "id", "actor_membership_id"),
        UniqueConstraint("organization_id", "user_message_id"),
        CheckConstraint("locale IN ('vi', 'en')", name="locale"),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'AWAITING_INPUT', 'AWAITING_HUMAN', "
            "'COMPLETED', 'FAILED')",
            name="status",
        ),
        Index(
            "ix_assistant_turns_conversation",
            "organization_id",
            "conversation_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    conversation_id: Mapped[UUID]
    user_message_id: Mapped[UUID]
    actor_membership_id: Mapped[UUID]
    objective: Mapped[str] = mapped_column(Text)
    locale: Mapped[str] = mapped_column(String(2))
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", server_default="QUEUED")
    safe_error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrchestrationRunModel(Base):
    __tablename__ = "orchestration_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "turn_id"],
            ["assistant_turns.organization_id", "assistant_turns.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "actor_membership_id"],
            ["memberships.organization_id", "memberships.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "turn_id", "actor_membership_id"],
            [
                "assistant_turns.organization_id",
                "assistant_turns.id",
                "assistant_turns.actor_membership_id",
            ],
        ),
        ForeignKeyConstraint(
            ["organization_id", "project_id", "report_id"],
            ["reports.organization_id", "reports.project_id", "reports.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "report_id", "base_version_id", "snapshot_id"],
            [
                "report_versions.organization_id",
                "report_versions.report_id",
                "report_versions.id",
                "report_versions.snapshot_id",
            ],
        ),
        ForeignKeyConstraint(
            ["organization_id", "report_id", "snapshot_id", "snapshot_hash"],
            [
                "report_metric_snapshots.organization_id",
                "report_metric_snapshots.report_id",
                "report_metric_snapshots.id",
                "report_metric_snapshots.snapshot_hash",
            ],
        ),
        ForeignKeyConstraint(
            ["organization_id", "project_id", "summary_id"],
            [
                "daily_summary_snapshots.organization_id",
                "daily_summary_snapshots.project_id",
                "daily_summary_snapshots.id",
            ],
        ),
        CheckConstraint(
            "(trigger_kind='CHAT_TURN' AND turn_id IS NOT NULL AND project_id IS NULL "
            "AND report_id IS NULL AND base_version_id IS NULL AND snapshot_id IS NULL "
            "AND snapshot_hash IS NULL AND summary_id IS NULL AND request_key IS NULL) OR "
            "(trigger_kind IN ('REPORT_REQUEST','SUMMARY_JOB') AND turn_id IS NULL "
            "AND project_id IS NOT NULL AND report_id IS NOT NULL AND base_version_id IS NOT NULL "
            "AND snapshot_id IS NOT NULL AND snapshot_hash IS NOT NULL "
            "AND length(request_key) BETWEEN 16 AND 128 AND request_key IS NOT NULL "
            "AND ((trigger_kind='REPORT_REQUEST' AND summary_id IS NULL) OR "
            "(trigger_kind='SUMMARY_JOB' AND summary_id IS NOT NULL)))",
            name="trigger_shape",
        ),
        Index(
            "uq_orchestration_trigger_request",
            "organization_id",
            "actor_membership_id",
            "request_key",
            unique=True,
            postgresql_where=text("trigger_kind<>'CHAT_TURN'"),
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "turn_id"),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'AWAITING_INPUT', 'AWAITING_HUMAN', "
            "'COMPLETED', 'FAILED')",
            name="status",
        ),
        Index("ix_orchestration_runs_status", "organization_id", "status", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    turn_id: Mapped[UUID | None]
    trigger_kind: Mapped[str] = mapped_column(String(24), server_default="CHAT_TURN")
    actor_membership_id: Mapped[UUID]
    project_id: Mapped[UUID | None]
    report_id: Mapped[UUID | None]
    base_version_id: Mapped[UUID | None]
    snapshot_id: Mapped[UUID | None]
    snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    summary_id: Mapped[UUID | None]
    request_key: Mapped[str | None] = mapped_column(String(128))
    orchestrator_version: Mapped[str] = mapped_column(String(64))
    orchestrator_fingerprint: Mapped[str] = mapped_column(String(128))
    execution_plan: Mapped[dict[str, Any]] = mapped_column(JSONB)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    budget: Mapped[dict[str, Any]] = mapped_column(JSONB)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", server_default="QUEUED")
    stop_reason: Mapped[str | None] = mapped_column(String(100))
    safe_error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentRunModel(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "orchestration_run_id"],
            ["orchestration_runs.organization_id", "orchestration_runs.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "parent_agent_run_id"],
            ["agent_runs.organization_id", "agent_runs.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "inbound_handoff_id"],
            ["agent_handoffs.organization_id", "agent_handoffs.id"],
            name="fk_agent_runs_inbound_handoff",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["organization_id", "workflow_run_id"],
            ["workflow_runs.organization_id", "workflow_runs.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "id"),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'AWAITING_INPUT', 'AWAITING_HUMAN', "
            "'COMPLETED', 'FAILED', 'CANCELLED')",
            name="status",
        ),
        Index(
            "ix_agent_runs_orchestration",
            "organization_id",
            "orchestration_run_id",
            "created_at",
            "id",
        ),
        Index("ix_agent_runs_workflow", "organization_id", "workflow_run_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    orchestration_run_id: Mapped[UUID]
    parent_agent_run_id: Mapped[UUID | None]
    inbound_handoff_id: Mapped[UUID | None]
    agent_id: Mapped[str] = mapped_column(String(100))
    agent_version: Mapped[str] = mapped_column(String(64))
    manifest_fingerprint: Mapped[str] = mapped_column(String(128))
    capability: Mapped[str] = mapped_column(String(100))
    typed_input: Mapped[dict[str, Any]] = mapped_column(JSONB)
    typed_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    version_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    budget: Mapped[dict[str, Any]] = mapped_column(JSONB)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", server_default="QUEUED")
    stop_reason: Mapped[str | None] = mapped_column(String(100))
    safe_error_code: Mapped[str | None] = mapped_column(String(100))
    workflow_run_id: Mapped[UUID | None]
    projected_workflow_sequence: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentHandoffModel(Base):
    __tablename__ = "agent_handoffs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "orchestration_run_id"],
            ["orchestration_runs.organization_id", "orchestration_runs.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "parent_agent_run_id"],
            ["agent_runs.organization_id", "agent_runs.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "orchestration_run_id", "dedupe_key"),
        Index(
            "ix_agent_handoffs_parent", "organization_id", "parent_agent_run_id", "created_at", "id"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    orchestration_run_id: Mapped[UUID]
    parent_agent_run_id: Mapped[UUID]
    target_agent_id: Mapped[str] = mapped_column(String(100))
    target_agent_version: Mapped[str] = mapped_column(String(64))
    capability: Mapped[str] = mapped_column(String(100))
    objective: Mapped[str] = mapped_column(Text)
    typed_input: Mapped[dict[str, Any]] = mapped_column(JSONB)
    context_references: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    budget: Mapped[dict[str, Any]] = mapped_column(JSONB)
    step_id: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    dedupe_key: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentCheckpointModel(Base):
    __tablename__ = "agent_checkpoints"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "orchestration_run_id"],
            ["orchestration_runs.organization_id", "orchestration_runs.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "agent_run_id"],
            ["agent_runs.organization_id", "agent_runs.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "agent_run_id", "sequence"),
        Index("ix_agent_checkpoints_run", "organization_id", "agent_run_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    orchestration_run_id: Mapped[UUID]
    agent_run_id: Mapped[UUID]
    sequence: Mapped[int] = mapped_column(Integer)
    node: Mapped[str] = mapped_column(String(100))
    typed_state: Mapped[dict[str, Any]] = mapped_column(JSONB)
    checkpoint_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentContextReferenceModel(Base):
    __tablename__ = "agent_context_references"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "agent_run_id"],
            ["agent_runs.organization_id", "agent_runs.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "agent_run_id", "resource_type", "resource_id"),
        Index("ix_agent_context_references_run", "organization_id", "agent_run_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    agent_run_id: Mapped[UUID]
    resource_type: Mapped[str] = mapped_column(String(100))
    resource_id: Mapped[UUID]
    resource_version: Mapped[int | None] = mapped_column(Integer)
    fingerprint: Mapped[str | None] = mapped_column(String(128))
    permission_scope: Mapped[str] = mapped_column(String(100))
    freshness_required: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SkillInvocationModel(Base):
    __tablename__ = "skill_invocations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "agent_run_id"],
            ["agent_runs.organization_id", "agent_runs.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "agent_run_id", "dedupe_key"),
        CheckConstraint("status IN ('RUNNING', 'SUCCEEDED', 'REJECTED', 'FAILED')", name="status"),
        Index("ix_skill_invocations_run", "organization_id", "agent_run_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    agent_run_id: Mapped[UUID]
    skill_id: Mapped[str] = mapped_column(String(100))
    skill_version: Mapped[str] = mapped_column(String(64))
    typed_input: Mapped[dict[str, Any]] = mapped_column(JSONB)
    typed_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="RUNNING", server_default="RUNNING")
    safe_error_code: Mapped[str | None] = mapped_column(String(100))
    dedupe_key: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ToolInvocationModel(Base):
    __tablename__ = "tool_invocations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "agent_run_id"],
            ["agent_runs.organization_id", "agent_runs.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "agent_run_id", "dedupe_key"),
        CheckConstraint("status IN ('RUNNING', 'SUCCEEDED', 'REJECTED', 'FAILED')", name="status"),
        Index("ix_tool_invocations_run", "organization_id", "agent_run_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    agent_run_id: Mapped[UUID]
    tool_id: Mapped[str] = mapped_column(String(100))
    tool_version: Mapped[str] = mapped_column(String(64))
    risk_level: Mapped[str] = mapped_column(String(30))
    typed_input: Mapped[dict[str, Any]] = mapped_column(JSONB)
    typed_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    context_references: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="RUNNING", server_default="RUNNING")
    idempotency_key: Mapped[str] = mapped_column(String(255))
    dedupe_key: Mapped[str] = mapped_column(String(255))
    safe_error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentModelInvocationModel(Base):
    __tablename__ = "agent_model_invocations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "agent_run_id"],
            ["agent_runs.organization_id", "agent_runs.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "agent_run_id", "invocation_key"),
        CheckConstraint("status IN ('SUCCEEDED', 'REJECTED', 'FAILED')", name="status"),
        Index(
            "ix_agent_model_invocations_run", "organization_id", "agent_run_id", "created_at", "id"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    agent_run_id: Mapped[UUID]
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(64))
    invocation_key: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    safe_error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AssistantEventModel(Base):
    __tablename__ = "assistant_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["assistant_conversations.organization_id", "assistant_conversations.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "turn_id"],
            ["assistant_turns.organization_id", "assistant_turns.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "orchestration_run_id"],
            ["orchestration_runs.organization_id", "orchestration_runs.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "agent_run_id"],
            ["agent_runs.organization_id", "agent_runs.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "conversation_id", "sequence"),
        UniqueConstraint("organization_id", "conversation_id", "dedupe_key"),
        UniqueConstraint("organization_id", "source_type", "source_id", "source_sequence"),
        CheckConstraint("sequence >= 1", name="sequence"),
        Index("ix_assistant_events_replay", "organization_id", "conversation_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    conversation_id: Mapped[UUID]
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(100))
    public_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    turn_id: Mapped[UUID | None]
    orchestration_run_id: Mapped[UUID | None]
    agent_run_id: Mapped[UUID | None]
    source_type: Mapped[str | None] = mapped_column(String(100))
    source_id: Mapped[UUID | None]
    source_sequence: Mapped[int | None] = mapped_column(Integer)
    dedupe_key: Mapped[str | None] = mapped_column(String(255))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AssistantJobModel(Base):
    __tablename__ = "assistant_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["assistant_conversations.organization_id", "assistant_conversations.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "turn_id"],
            ["assistant_turns.organization_id", "assistant_turns.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "orchestration_run_id"],
            ["orchestration_runs.organization_id", "orchestration_runs.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "requester_membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "orchestration_run_id", "job_type"),
        CheckConstraint(
            "job_type IN ('assistant.turn.execute', 'assistant.conversation.title')",
            name="job_type",
        ),
        Index(
            "uq_assistant_jobs_conversation_title",
            "organization_id",
            "conversation_id",
            unique=True,
            postgresql_where=text("job_type = 'assistant.conversation.title'"),
        ),
        CheckConstraint("status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')", name="status"),
        CheckConstraint("attempt_count >= 0 AND attempt_count <= max_attempts", name="attempts"),
        Index(
            "ix_assistant_jobs_queue",
            "organization_id",
            "status",
            "available_at",
            "lease_until",
            "id",
        ),
        Index("ix_assistant_jobs_lease", "organization_id", "locked_by", "lease_until", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    conversation_id: Mapped[UUID]
    turn_id: Mapped[UUID]
    orchestration_run_id: Mapped[UUID]
    requester_membership_id: Mapped[UUID]
    job_type: Mapped[str] = mapped_column(String(100), server_default="assistant.turn.execute")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", server_default="QUEUED")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    locked_by: Mapped[str | None] = mapped_column(String(255))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    safe_error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
