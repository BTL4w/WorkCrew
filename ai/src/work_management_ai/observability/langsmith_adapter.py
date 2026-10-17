"""Bounded, best-effort export of allowlisted spans; no SDK auto instrumentation."""

import asyncio
import logging
import queue
import threading
from time import monotonic
from typing import Protocol, override

from work_management_ai.observability.safe_io import sanitize_structured_io
from work_management_ai.observability.safe_trace import TraceSpanRecord
from work_management_ai.tracing import TraceMetadata, trace_span

logger = logging.getLogger(__name__)


class TraceClient(Protocol):
    def create_run(self, **kwargs: object) -> None: ...


class _SDKLogFilter(logging.Filter):
    """SDK transport logs may contain raw server responses or credentials."""

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        return False


def silence_sdk_transport_logs() -> None:
    for name in ("langsmith.client", "langsmith.utils", "langsmith._internal._background_thread"):
        logging.getLogger(name).addFilter(_SDKLogFilter())


class LangSmithTraceAdapter:
    def __init__(
        self,
        *,
        client: TraceClient,
        address: str,
        queue_size: int = 256,
        hide_inputs: bool = True,
        hide_outputs: bool = True,
    ) -> None:
        self._client = client
        self._address = address
        self._hide_inputs, self._hide_outputs = hide_inputs, hide_outputs
        self._queue: queue.Queue[TraceSpanRecord] = queue.Queue(maxsize=queue_size)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._closed = False

    def emit(self, record: TraceSpanRecord) -> None:
        if self._closed:
            return
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._drain, name="safe-langsmith-export", daemon=True
            )
            self._thread.start()
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            logger.warning("LANGSMITH_EXPORT_QUEUE_FULL")

    def _drain(self) -> None:
        while not self._stop.is_set():
            try:
                record = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                self._send(record)
            except Exception:
                logger.warning("LANGSMITH_EXPORT_FAILED")
            finally:
                self._queue.task_done()

    def _send(self, record: TraceSpanRecord) -> None:
        metadata = record.model_dump(
            mode="json", exclude_none=True, exclude={"input_data", "output_data"}
        )
        for key in ("id", "trace_id", "parent_run_id", "start_time", "end_time", "dotted_order"):
            metadata.pop(key, None)
        metadata["io_filter_version"] = "1.0.0"
        metadata["io_projection"] = "structured_allowlist"
        name = {
            "assistant": "assistant.turn",
            "agent": f"agent.{record.agent_id}" if record.agent_id else "agent",
            "model": "model",
            "tool": "tool",
            "workflow": "workflow.node",
        }[record.kind]
        self._client.create_run(
            id=record.id,
            name=name,
            inputs={}
            if self._hide_inputs
            else {
                **record.model_dump(
                    mode="json",
                    exclude_none=True,
                    include={
                        "kind",
                        "organization_id",
                        "orchestration_run_id",
                        "agent_run_id",
                        "agent_id",
                        "agent_version",
                        "tool_id",
                        "tool_version",
                        "model_reference",
                        "workflow_node",
                        "workflow_version",
                        "prompt_version",
                    },
                ),
                "data": sanitize_structured_io(record.input_data),
            },
            outputs={}
            if self._hide_outputs
            else {
                **record.model_dump(
                    mode="json",
                    exclude_none=True,
                    include={
                        "status",
                        "input_tokens",
                        "output_tokens",
                        "verifier_passed",
                        "verifier_version",
                    },
                ),
                "data": sanitize_structured_io(record.output_data),
            },
            run_type="llm"
            if record.kind == "model"
            else "tool"
            if record.kind == "tool"
            else "chain",
            trace_id=record.trace_id,
            parent_run_id=record.parent_run_id,
            dotted_order=record.dotted_order,
            start_time=record.start_time,
            end_time=record.end_time,
            error="EXECUTION_FAILED"
            if record.status in {"FAILED", "CANCELLED", "REJECTED"}
            else None,
            extra={"metadata": metadata},
            address=self._address,
        )

    async def record(self, metadata: TraceMetadata) -> None:
        """Legacy planning nodes attach only when a tenant-scoped session exists."""
        async with trace_span(
            "workflow",
            workflow_node=metadata.node,
            workflow_version=metadata.workflow_version,
            prompt_version=metadata.prompt_version,
            verifier_version=metadata.verifier_version,
        ) as outcome:
            if metadata.outcome == "FAILED":
                outcome.status = "FAILED"
            elif metadata.outcome == "INTERRUPTED":
                outcome.status = "AWAITING_HUMAN"

    async def close(self) -> None:
        self._closed = True
        if self._thread is None:
            return
        deadline = monotonic() + 3
        while self._queue.unfinished_tasks:
            if monotonic() >= deadline:
                logger.warning("LANGSMITH_EXPORT_FLUSH_TIMEOUT")
                break
            await asyncio.sleep(0.01)
        self._stop.set()
