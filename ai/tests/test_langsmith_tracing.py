"""Offline safety and lifecycle tests for manual LangSmith tracing."""

import asyncio
import logging
import subprocess
import sys
from uuid import uuid4

import pytest
from langsmith.run_helpers import get_tracing_context

from work_management_ai.observability.langsmith_adapter import LangSmithTraceAdapter
from work_management_ai.tracing import trace_session, trace_span


class FakeClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.runs: list[dict[str, object]] = []
        self.fail = fail

    def create_run(self, **kwargs: object) -> None:
        if self.fail:
            raise RuntimeError("secret-provider-body")
        self.runs.append(kwargs)


@pytest.mark.asyncio
async def test_nested_trace_exports_only_safe_metadata_and_preserves_parentage() -> None:
    client = FakeClient()
    adapter = LangSmithTraceAdapter(client=client, address="lrn:agents/workcrew/environments/local")
    tenant, run = uuid4(), uuid4()
    async with trace_session(adapter, organization_id=tenant, orchestration_run_id=run):
        assert get_tracing_context()["enabled"] is False
        async with (
            trace_span("agent", agent_id="orchestrator"),
            trace_span("agent", agent_id="planning"),
            trace_span("model") as span,
        ):
            span.input_tokens = 12
            span.output_tokens = 4
    await adapter.close()
    assert len(client.runs) == 4
    by_name = {str(item["name"]): item for item in client.runs}
    root = by_name["assistant.turn"]
    orchestrator = by_name["agent.orchestrator"]
    planning = by_name["agent.planning"]
    model = by_name["model"]
    assert orchestrator["parent_run_id"] == root["id"]
    assert planning["parent_run_id"] == orchestrator["id"]
    assert model["parent_run_id"] == planning["id"]
    for item in client.runs:
        assert item["inputs"] == {} and item["outputs"] == {}
        assert item["trace_id"] == root["id"]
        assert item["address"] == "lrn:agents/workcrew/environments/local"
        assert str(tenant) in str(item["extra"])


@pytest.mark.asyncio
async def test_failure_is_sanitized_and_original_business_error_is_preserved() -> None:
    client = FakeClient()
    adapter = LangSmithTraceAdapter(client=client, address="lrn:agents/workcrew/environments/local")
    with pytest.raises(RuntimeError, match="private business text"):
        async with trace_session(adapter, organization_id=uuid4(), orchestration_run_id=uuid4()):
            raise RuntimeError("private business text")
    await adapter.close()
    assert "private business text" not in str(client.runs)
    assert client.runs[0]["error"] == "EXECUTION_FAILED"


@pytest.mark.asyncio
async def test_cross_tenant_child_is_not_exported_or_attached() -> None:
    client = FakeClient()
    adapter = LangSmithTraceAdapter(client=client, address="lrn:agents/workcrew/environments/local")
    foreign = uuid4()
    async with (
        trace_session(adapter, organization_id=uuid4(), orchestration_run_id=uuid4()),
        trace_span("agent", organization_id=foreign, agent_id="planning"),
        trace_span("model"),
    ):
        pass
    await adapter.close()
    assert len(client.runs) == 1
    assert str(foreign) not in str(client.runs)


@pytest.mark.asyncio
async def test_outage_and_queue_overflow_do_not_block_business_or_leak_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = LangSmithTraceAdapter(
        client=FakeClient(fail=True), address="lrn:agents/workcrew/environments/local", queue_size=1
    )
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            async with trace_session(
                adapter, organization_id=uuid4(), orchestration_run_id=uuid4()
            ):
                pass
        await adapter.close()
    assert "secret-provider-body" not in caplog.text
    assert "LANGSMITH_EXPORT" in caplog.text


@pytest.mark.asyncio
async def test_concurrent_tenants_have_independent_trace_trees() -> None:
    client = FakeClient()
    adapter = LangSmithTraceAdapter(client=client, address="lrn:agents/workcrew/environments/local")

    async def execute() -> None:
        async with trace_session(adapter, organization_id=uuid4(), orchestration_run_id=uuid4()):
            await asyncio.sleep(0)
            async with trace_span("tool", tool_id="work.read_my_tasks", tool_version="1.0.0"):
                pass

    await asyncio.gather(execute(), execute())
    await adapter.close()
    assert len(client.runs) == 4
    roots = {item["id"]: item for item in client.runs if item["parent_run_id"] is None}
    for child in (item for item in client.runs if item["parent_run_id"] is not None):
        parent = roots[child["parent_run_id"]]
        assert child["extra"] == parent["extra"] or child["trace_id"] == parent["id"]


@pytest.mark.asyncio
async def test_disabled_tracing_never_calls_client_and_restores_context() -> None:
    before = get_tracing_context()
    async with (
        trace_session(None, organization_id=uuid4(), orchestration_run_id=uuid4()),
        trace_span("model"),
    ):
        pass
    assert get_tracing_context() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hide_inputs", "hide_outputs"), [(False, False), (False, True), (True, False), (True, True)]
)
async def test_visibility_switches_apply_independently_to_safe_payloads(
    hide_inputs: bool,
    hide_outputs: bool,
) -> None:
    client = FakeClient()
    adapter = LangSmithTraceAdapter(
        client=client,
        address="lrn:agents/workcrew/environments/local",
        hide_inputs=hide_inputs,
        hide_outputs=hide_outputs,
    )
    async with (
        trace_session(adapter, organization_id=uuid4(), orchestration_run_id=uuid4()),
        trace_span("agent", agent_id="planning", agent_version="1.0.0") as span,
    ):
        span.capture_inputs({"typed_input": {"task_id": str(uuid4()), "message": "private prompt"}})
        span.capture_outputs({"typed_output": {"progress_percent": 50, "reasoning": "hidden text"}})
    await adapter.close()
    planning = next(row for row in client.runs if row["name"] == "agent.planning")
    assert bool(planning["inputs"]) is not hide_inputs
    assert bool(planning["outputs"]) is not hide_outputs
    if not hide_inputs:
        assert "planning" in str(planning["inputs"])
    if not hide_outputs:
        assert "COMPLETED" in str(planning["outputs"])
        assert "progress_percent" in str(planning["outputs"])
    assert "private prompt" not in str(client.runs)
    assert "hidden text" not in str(client.runs)
    assert "input_data" not in str(planning["extra"])
    assert "output_data" not in str(planning["extra"])


def test_blocked_export_does_not_hold_process_shutdown() -> None:
    script = """
import asyncio
import time
from uuid import uuid4
from work_management_ai.observability.langsmith_adapter import LangSmithTraceAdapter
from work_management_ai.tracing import trace_session
class Client:
    def create_run(self, **kwargs):
        time.sleep(20)
async def main():
    adapter = LangSmithTraceAdapter(
        client=Client(), address='lrn:agents/workcrew/environments/local'
    )
    async with trace_session(adapter, organization_id=uuid4(), orchestration_run_id=uuid4()):
        pass
    await asyncio.sleep(0.1)
    await adapter.close()
asyncio.run(main())
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        timeout=6,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0
