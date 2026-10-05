"""Regression tests for the read-only Project-only flow diagnostic."""

from typing import Any

from app.scripts.diagnose_project_only import (
    describe_block,
    inspect_snapshot,
    latest_user_sequence,
)


def test_describe_block_does_not_leak_free_text() -> None:
    block = {
        "kind": "text",
        "text": "private prompt and model response",
    }
    assert describe_block(block) == "text"


def test_inspect_snapshot_detects_project_proposal() -> None:
    snapshot: dict[str, Any] = {
        "conversation": {"status": "ACTIVE", "last_event_sequence": 7},
        "messages": [
            {"sequence": 1, "role": "USER", "content_blocks": [{"kind": "text", "text": "secret"}]},
            {
                "sequence": 2,
                "role": "ASSISTANT",
                "content_blocks": [
                    {"kind": "activity", "status": "COMPLETED", "label_key": "ai.planning"},
                    {
                        "kind": "proposal",
                        "proposal_id": "p1",
                        "workflow_run_id": "w1",
                        "state": "PROPOSED",
                    },
                ],
            },
        ],
    }
    result = inspect_snapshot(snapshot, after_sequence=0)
    assert result.last_sequence == 2
    assert result.outcome == "PROPOSAL_READY"
    assert result.lines == (
        "message #2: activity status=COMPLETED label_key=ai.planning",
        "message #2: proposal state=PROPOSED workflow_run_id=w1 proposal_id=p1",
    )


def test_inspect_snapshot_reports_error_and_wrong_team_branch() -> None:
    snapshot: dict[str, Any] = {
        "conversation": {},
        "messages": [
            {
                "sequence": 3,
                "role": "ASSISTANT",
                "content_blocks": [
                    {
                        "kind": "safe_error",
                        "code": "MODEL_UNAVAILABLE",
                        "message_key": "ai.error.unavailable",
                    },
                ],
            },
            {
                "sequence": 4,
                "role": "ASSISTANT",
                "content_blocks": [
                    {"kind": "team_recommendation", "recommendation_id": "r1"},
                ],
            },
        ],
    }
    assert inspect_snapshot(snapshot, after_sequence=2).outcome == "UNEXPECTED_TEAM_BRANCH"
    assert inspect_snapshot(snapshot, after_sequence=3).outcome == "UNEXPECTED_TEAM_BRANCH"


def test_inspect_snapshot_ignores_old_messages() -> None:
    snapshot: dict[str, Any] = {
        "conversation": {},
        "messages": [
            {"sequence": 1, "role": "ASSISTANT", "content_blocks": [{"kind": "proposal"}]},
            {
                "sequence": 2,
                "role": "ASSISTANT",
                "content_blocks": [{"kind": "planning_run", "status": "RUNNING"}],
            },
        ],
    }
    result = inspect_snapshot(snapshot, after_sequence=1)
    assert result.outcome is None
    assert result.lines == ("message #2: planning_run status=RUNNING",)


def test_latest_user_sequence_skips_previous_proposal() -> None:
    snapshot: dict[str, Any] = {
        "messages": [
            {"sequence": 1, "role": "USER"},
            {"sequence": 2, "role": "ASSISTANT", "content_blocks": [{"kind": "proposal"}]},
            {"sequence": 3, "role": "USER"},
        ]
    }
    assert latest_user_sequence(snapshot) == 3
