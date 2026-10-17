"""Runtime trace boundaries exclude untrusted invocation names."""

from uuid import uuid4

import pytest

from app.modules.assistant.adapters.agent_runtime import build_agent_registry
from app.modules.assistant.adapters.work_tools import RecordingToolExecutor
from work_management_ai.observability.langsmith_adapter import LangSmithTraceAdapter
from work_management_ai.runtime.contracts import ActorReference, ToolExecutionRequest
from work_management_ai.tracing import trace_session


@pytest.mark.asyncio
async def test_rejected_tool_name_cannot_export_business_text() -> None:
    class Client:
        def __init__(self) -> None:
            self.runs: list[dict[str, object]] = []

        def create_run(self, **kwargs: object) -> None:
            self.runs.append(kwargs)

    client = Client()
    adapter = LangSmithTraceAdapter(client=client, address="lrn:agents/workcrew/environments/local")
    _, registry = build_agent_registry()
    executor = RecordingToolExecutor(
        transaction_factory=object(),  # type: ignore[arg-type]
        tool_registry=registry,
        backend=object(),  # type: ignore[arg-type]
    )
    tenant = uuid4()
    async with trace_session(adapter, organization_id=tenant, orchestration_run_id=uuid4()):
        result = await executor.execute(
            ToolExecutionRequest(
                agent_run_id=uuid4(),
                tool_id="customer-acme-financial-records",
                tool_version="1.0.0",
                call_id="call",
                idempotency_key="call",
                actor=ActorReference(organization_id=tenant, membership_id=uuid4()),
                typed_input={},
            )
        )
    await adapter.close()
    assert result.status == "REJECTED"
    assert "customer-acme-financial-records" not in str(client.runs)
