"""Structured trace I/O accepts only bounded, recognized scalar fields."""

import json
from uuid import uuid4

from work_management_ai.observability.safe_io import sanitize_structured_io


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
