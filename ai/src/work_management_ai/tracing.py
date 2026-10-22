"""Redacted, non-blocking tracing boundary for AI workflows."""

import asyncio
import logging
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID, uuid4

from langsmith import tracing_context  # pyright: ignore[reportUnknownVariableType]
from pydantic import BaseModel

from work_management_ai.observability.safe_io import sanitize_structured_io
from work_management_ai.observability.safe_trace import TraceSpanRecord
from work_management_ai.runtime.contracts import JsonValue

logger = logging.getLogger(__name__)


class SpanSink(Protocol):
    def emit(self, record: TraceSpanRecord) -> None: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class SpanOutcome:
    status: Literal[
        "COMPLETED", "FAILED", "CANCELLED", "REJECTED", "AWAITING_INPUT", "AWAITING_HUMAN"
    ] = "COMPLETED"
    input_tokens: int | None = None
    output_tokens: int | None = None
    verifier_passed: bool | None = None
    model_reference: str | None = None
    input_data: dict[str, JsonValue] = field(default_factory=lambda: dict[str, JsonValue]())
    output_data: dict[str, JsonValue] = field(default_factory=lambda: dict[str, JsonValue]())

    def capture_inputs(self, value: Mapping[str, object]) -> None:
        if _SCOPE.get() is None:
            return
        try:
            self.input_data = sanitize_structured_io(value)
        except Exception:
            logger.warning("TRACE_INPUT_CAPTURE_FAILED")

    def capture_outputs(
        self, value: Mapping[str, object] | BaseModel, *, parsed: bool = False
    ) -> None:
        if _SCOPE.get() is None:
            return
        try:
            data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
            self.output_data = sanitize_structured_io({"parsed": data} if parsed else data)
        except Exception:
            logger.warning("TRACE_OUTPUT_CAPTURE_FAILED")

    def set_status(self, status: str) -> None:
        statuses: dict[
            str,
            Literal[
                "COMPLETED", "FAILED", "CANCELLED", "REJECTED", "AWAITING_INPUT", "AWAITING_HUMAN"
            ],
        ] = {
            "FAILED": "FAILED",
            "CANCELLED": "CANCELLED",
            "REJECTED": "REJECTED",
            "AWAITING_INPUT": "AWAITING_INPUT",
            "AWAITING_HUMAN": "AWAITING_HUMAN",
        }
        self.status = statuses.get(status, "COMPLETED")


@dataclass(frozen=True, slots=True)
class _Scope:
    sink: SpanSink
    organization_id: UUID
    orchestration_run_id: UUID
    span_id: UUID
    trace_id: UUID
    dotted_order: str


_SCOPE: ContextVar[_Scope | None] = ContextVar("safe_trace_scope", default=None)


@asynccontextmanager
async def trace_session(
    sink: SpanSink | None, *, organization_id: UUID, orchestration_run_id: UUID
) -> AsyncGenerator[SpanOutcome]:
    """Start a fresh attempt tree; durable run identity remains in metadata."""
    token = _SCOPE.set(None)
    try:
        with tracing_context(enabled=False):
            async with trace_span(
                "assistant",
                organization_id=organization_id,
                orchestration_run_id=orchestration_run_id,
                sink=sink,
            ) as outcome:
                yield outcome
    finally:
        _SCOPE.reset(token)


@asynccontextmanager
async def trace_span(
    kind: Literal["assistant", "agent", "model", "tool", "workflow"],
    *,
    organization_id: UUID | None = None,
    orchestration_run_id: UUID | None = None,
    agent_run_id: UUID | None = None,
    agent_id: Literal[
        "orchestrator",
        "work_intelligence",
        "planning",
        "assignment",
        "daily_update",
        "risk",
        "reporting",
        "retrieve",
    ]
    | None = None,
    agent_version: str | None = None,
    tool_id: str | None = None,
    tool_version: str | None = None,
    workflow_node: str | None = None,
    workflow_version: str | None = None,
    prompt_version: str | None = None,
    verifier_version: str | None = None,
    sink: SpanSink | None = None,
) -> AsyncGenerator[SpanOutcome]:
    parent = _SCOPE.get()
    outcome = SpanOutcome()
    if parent is not None:
        if (organization_id is not None and organization_id != parent.organization_id) or (
            orchestration_run_id is not None and orchestration_run_id != parent.orchestration_run_id
        ):
            token = _SCOPE.set(None)
            try:
                yield outcome
            finally:
                _SCOPE.reset(token)
            return
        organization_id = parent.organization_id
        orchestration_run_id = parent.orchestration_run_id
        sink = parent.sink
    if sink is None or organization_id is None or orchestration_run_id is None:
        yield outcome
        return
    started = datetime.now(UTC)
    span_id = uuid4()
    order = started.strftime("%Y%m%dT%H%M%S%fZ") + str(span_id)
    if parent is not None:
        order = parent.dotted_order + "." + order
    trace_id = parent.trace_id if parent is not None else span_id
    token = _SCOPE.set(
        _Scope(sink, organization_id, orchestration_run_id, span_id, trace_id, order)
    )
    try:
        yield outcome
    except asyncio.CancelledError:
        outcome.status = "CANCELLED"
        raise
    except BaseException:
        outcome.status = "FAILED"
        raise
    finally:
        _SCOPE.reset(token)
        try:
            sink.emit(
                TraceSpanRecord(
                    id=span_id,
                    trace_id=trace_id,
                    parent_run_id=parent.span_id if parent is not None else None,
                    organization_id=organization_id,
                    orchestration_run_id=orchestration_run_id,
                    agent_run_id=agent_run_id,
                    kind=kind,
                    agent_id=agent_id,
                    agent_version=agent_version,
                    tool_id=tool_id,
                    tool_version=tool_version,
                    workflow_node=workflow_node,
                    workflow_version=workflow_version,
                    prompt_version=prompt_version,
                    verifier_version=verifier_version,
                    start_time=started,
                    end_time=datetime.now(UTC),
                    dotted_order=order,
                    status=outcome.status,
                    input_tokens=outcome.input_tokens,
                    output_tokens=outcome.output_tokens,
                    verifier_passed=outcome.verifier_passed,
                    model_reference=outcome.model_reference,
                    input_data=outcome.input_data,
                    output_data=outcome.output_data,
                )
            )
        except Exception:
            logger.warning("LANGSMITH_EXPORT_RECORD_REJECTED")


@dataclass(frozen=True, slots=True)
class TraceMetadata:
    """Safe metadata only; prompts, provider errors and reasoning are excluded."""

    run_id: UUID
    node: str
    outcome: Literal["STARTED", "COMPLETED", "INTERRUPTED", "FAILED"]
    workflow_version: str
    prompt_version: str
    schema_version: str
    verifier_version: str
    model_reference: str | None = None
    error_code: str | None = None


class TracePort(Protocol):
    """Optional observational sink that cannot affect workflow decisions."""

    async def record(self, metadata: TraceMetadata) -> None: ...


class NoopTracePort:
    """Default trace adapter for local and automated execution."""

    async def record(self, metadata: TraceMetadata) -> None:
        del metadata


async def record_safely(port: TracePort, metadata: TraceMetadata) -> None:
    """Isolate tracing/LangSmith outages without logging sensitive details."""

    try:
        await port.record(metadata)
    except Exception:
        logger.warning(
            "AI trace sink failed for run=%s node=%s",
            metadata.run_id,
            metadata.node,
        )
