"""Allowlisted metadata-only trace serialization."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from work_management_ai.runtime.contracts import JsonValue


def _reject_sensitive_identifier(value: str) -> str:
    lowered = value.casefold()
    forbidden = (
        "sk-",
        "secret",
        "token",
        "password",
        "prompt",
        "reasoning",
        "chain_of_thought",
        "error",
        "exception",
        "traceback",
        "postgresql",
        "private",
    )
    if any(fragment in lowered for fragment in forbidden):
        raise ValueError("trace identifier contains a forbidden sensitive fragment")
    return value


_VERSION = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}$"),
    AfterValidator(_reject_sensitive_identifier),
]
_SAFE_CODE = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,63}$"),
]


class SafeTraceRecord(BaseModel):
    """A trace record that cannot carry prompts, secrets or raw errors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    orchestration_run_id: UUID
    agent_run_id: UUID | None = None
    agent_id: Literal[
        "orchestrator",
        "work_intelligence",
        "planning",
        "assignment",
        "daily_update",
        "risk",
        "reporting",
    ]
    agent_version: _VERSION
    workflow_version: _VERSION
    prompt_version: _VERSION
    verifier_versions: tuple[_VERSION, ...]
    status: Literal[
        "QUEUED",
        "RUNNING",
        "AWAITING_INPUT",
        "AWAITING_HUMAN",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
    ]
    iteration_count: int = Field(ge=0, le=16)
    tool_call_count: int = Field(ge=0, le=32)
    handoff_count: int = Field(ge=0, le=16)
    duration_ms: int = Field(ge=0)
    safe_codes: tuple[_SAFE_CODE, ...] = ()
    # Only opaque IDs are trace-safe. Human-readable resource names, URLs and
    # exception strings belong in protected business/audit stores, not traces.
    evidence_references: tuple[UUID, ...] = ()


def serialize_safe_trace(record: SafeTraceRecord) -> dict[str, object]:
    """Serialize only the explicitly allowlisted safe trace schema."""

    return record.model_dump(mode="json")


class TraceSpanRecord(BaseModel):
    """Operational span; structured I/O is filtered again at the export boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    trace_id: UUID
    parent_run_id: UUID | None
    organization_id: UUID
    orchestration_run_id: UUID
    agent_run_id: UUID | None = None
    kind: Literal["assistant", "agent", "model", "tool", "workflow"]
    agent_id: (
        Literal[
            "orchestrator",
            "work_intelligence",
            "planning",
            "assignment",
            "daily_update",
            "risk",
            "reporting",
        ]
        | None
    ) = None
    agent_version: _VERSION | None = None
    model_reference: (
        Annotated[
            str,
            StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,95}$"),
            AfterValidator(_reject_sensitive_identifier),
        ]
        | None
    ) = None
    tool_id: _VERSION | None = None
    tool_version: _VERSION | None = None
    workflow_node: _VERSION | None = None
    workflow_version: _VERSION | None = None
    prompt_version: _VERSION | None = None
    verifier_version: _VERSION | None = None
    start_time: datetime
    end_time: datetime
    dotted_order: str = Field(pattern=r"^[0-9TZ.a-f-]+$")
    status: Literal[
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "REJECTED",
        "AWAITING_INPUT",
        "AWAITING_HUMAN",
    ] = "COMPLETED"
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    verifier_passed: bool | None = None
    input_data: dict[str, JsonValue] = Field(default_factory=dict)
    output_data: dict[str, JsonValue] = Field(default_factory=dict)
