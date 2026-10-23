"""Structured trace I/O accepts only bounded, recognized scalar fields."""

import json
from uuid import uuid4

import pytest

from work_management_ai.observability.safe_io import sanitize_structured_io


@pytest.mark.asyncio
async def test_retrieval_external_capture_exports_metadata_only():
    from work_management_ai.observability.langsmith_adapter import LangSmithTraceAdapter
    from work_management_ai.tracing import trace_session, trace_span

    class Client:
        def __init__(self):
            self.runs: list[dict[str, object]] = []

        def create_run(self, **kwargs: object):
            self.runs.append(kwargs)

    client = Client()
    adapter = LangSmithTraceAdapter(
        client=client,
        address="lrn:agents/workcrew/environments/local",
        hide_inputs=False,
        hide_outputs=False,
    )
    source_id = str(uuid4())
    async with (
        trace_session(adapter, organization_id=uuid4(), orchestration_run_id=uuid4()),
        trace_span("tool", tool_id="web.search", tool_version="1.0.0", agent_id="retrieve") as span,
    ):
        span.capture_inputs({"typed_input": {"query": "raw-web-query-marker", "limit": 5}})
        span.capture_outputs(
            {
                "typed_output": {
                    "source_count": 1,
                    "provider_requests": 1,
                    "cost_usd": 0.005,
                    "workflow_version": "retrieve.web.v1",
                    "evidence_id": source_id,
                    "query": "raw-web-query-marker",
                    "canonical_url": "https://example.org/raw-url-marker",
                    "excerpt": "raw-source-text-marker",
                    "api_key": "sk-raw-key-marker",
                    "error": "raw-exception-marker",
                    "title": "raw-title-marker",
                }
            }
        )
    await adapter.close()
    wire = json.dumps(client.runs, default=str)
    for marker in (
        "raw-web-query-marker",
        "raw-url-marker",
        "raw-source-text-marker",
        "sk-raw-key-marker",
        "raw-exception-marker",
        "raw-title-marker",
    ):
        assert marker not in wire
    assert source_id in wire
    output = client.runs[0]["outputs"]
    assert isinstance(output, dict)
    assert output["data"] == {
        "typed_output": {
            "source_count": 1,
            "provider_requests": 1,
            "cost_usd": 0.005,
            "workflow_version": "retrieve.web.v1",
            "evidence_id": source_id,
        }
    }


def test_safe_operational_fields_survive_but_free_text_and_secrets_do_not() -> None:
    task_id = str(uuid4())
    sanitized = sanitize_structured_io(
        {
            "task_id": task_id,
            "limit": 10,
            "status": "COMPLETED",
            "locale": "vi",
            "message": "private user prompt",
            "system_prompt": "private instructions",
            "reasoning": "hidden reasoning",
            "api_key": "sk-private-key",
            "error": "raw error",
            "typed_output": {"progress_percent": 50, "description": "private description"},
        }
    )
    assert sanitized == {
        "task_id": task_id,
        "limit": 10,
        "status": "COMPLETED",
        "locale": "vi",
        "typed_output": {"progress_percent": 50},
    }


def test_strings_disguised_as_identifiers_or_status_do_not_pass() -> None:
    assert (
        sanitize_structured_io(
            {
                "task_id": "private title",
                "status": "secret-provider-body",
                "agent_id": "private business name",
                "limit": float("inf"),
            }
        )
        == {}
    )


def test_recursive_input_terminates_and_large_arrays_are_bounded() -> None:
    cycle: dict[str, object] = {}
    cycle["typed_input"] = cycle
    assert sanitize_structured_io(cycle) == {}
    projected = sanitize_structured_io({"items": [{"count": i} for i in range(100)]})
    assert isinstance(projected["items"], list)
    assert len(projected["items"]) == 32


def test_broad_nested_payload_cannot_create_a_large_export() -> None:
    value: dict[str, object] = {"task_id": str(uuid4())}
    for _ in range(3):
        value = {"items": [value] * 32}
    assert len(json.dumps(sanitize_structured_io(value))) < 64_000
