"""Deny-by-default, bounded operational I/O projection; never exports free text."""

import math
from collections.abc import Mapping
from contextlib import suppress
from itertools import islice
from typing import cast
from uuid import UUID

from work_management_ai.runtime.contracts import JsonValue

_IDS = frozenset(
    {
        "id",
        "task_id",
        "project_id",
        "draft_id",
        "proposal_id",
        "workflow_run_id",
        "agent_run_id",
        "orchestration_run_id",
        "conversation_id",
        "turn_id",
        "membership_id",
    }
)
_NUMBERS = frozenset(
    {
        "limit",
        "count",
        "version",
        "draft_version",
        "proposal_version",
        "duration_ms",
        "input_tokens",
        "output_tokens",
        "message_count",
        "system_message_count",
        "non_system_message_count",
        "max_iterations",
        "max_tool_calls",
        "max_model_attempts",
        "max_input_tokens",
        "max_output_tokens",
        "timeout_seconds",
        "progress_percent",
        "remaining_effort",
        "iterations_used",
        "tool_calls_used",
        "model_attempts_used",
    }
)
_BOOLS = frozenset({"requires_human_gate", "verifier_passed", "passed", "truncated"})
_CONTAINERS = frozenset(
    {
        "typed_input",
        "typed_output",
        "parsed",
        "steps",
        "agent_results",
        "tasks",
        "items",
        "verifier_results",
        "proposed_actions",
        "budget",
    }
)
_ENUMS: dict[str, frozenset[str]] = {
    "locale": frozenset({"vi", "en"}),
    "agent_id": frozenset(
        {
            "orchestrator",
            "work_intelligence",
            "planning",
            "assignment",
            "daily_update",
            "risk",
            "reporting",
        }
    ),
    "target_agent_id": frozenset(
        {
            "orchestrator",
            "work_intelligence",
            "planning",
            "assignment",
            "daily_update",
            "risk",
            "reporting",
        }
    ),
    "status": frozenset(
        {
            "QUEUED",
            "RUNNING",
            "COMPLETED",
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "REJECTED",
            "AWAITING_INPUT",
            "AWAITING_HUMAN",
        }
    ),
    "interaction_outcome": frozenset(
        {"NEEDS_INPUT", "DRAFT_READY", "RESULT_READY", "ACTION_REVIEW_REQUIRED", "FAILED"}
    ),
    "decision": frozenset({"ASK_GOAL", "ASK_DECISIVE", "DRAFT", "CONFIRM_SCOPE", "UNSUPPORTED"}),
}


def sanitize_structured_io(
    value: Mapping[str, object],
    *,
    depth: int = 0,
    _budget: list[int] | None = None,
) -> dict[str, JsonValue]:
    budget = _budget if _budget is not None else [512]
    if depth >= 4 or budget[0] <= 0:
        return {}
    result: dict[str, JsonValue] = {}
    for key, item in islice(value.items(), 64):
        if budget[0] <= 0:
            break
        budget[0] -= 1
        if key in _IDS and isinstance(item, (UUID, str)):
            with suppress(ValueError):
                result[key] = str(UUID(str(item)))
        elif key in _NUMBERS and type(item) in (int, float):
            number = cast(int | float, item)
            if 0 <= number <= 1_000_000_000 and math.isfinite(number):
                result[key] = number
        elif (key in _BOOLS and isinstance(item, bool)) or (
            key in _ENUMS and isinstance(item, str) and item in _ENUMS[key]
        ):
            result[key] = item
        elif key in _CONTAINERS:
            if isinstance(item, Mapping):
                nested = sanitize_structured_io(
                    cast(Mapping[str, object], item), depth=depth + 1, _budget=budget
                )
                if nested:
                    result[key] = nested
            elif isinstance(item, (tuple, list)):
                rows: list[JsonValue] = []
                for row in cast(list[object], item)[:32]:
                    if isinstance(row, Mapping):
                        nested = sanitize_structured_io(
                            cast(Mapping[str, object], row), depth=depth + 1, _budget=budget
                        )
                        if nested:
                            rows.append(nested)
                if rows:
                    result[key] = rows
    return result
