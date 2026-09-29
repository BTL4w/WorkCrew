"""Reporting invariants remain deterministic when assessment is unavailable."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.modules.progress.domain.daily_updates import (
    DailyUpdateError,
    DailyUpdateItemInput,
    validate_items,
)


def item(**changes: object) -> DailyUpdateItemInput:
    return DailyUpdateItemInput.model_validate(
        {
            "task_id": str(uuid4()),
            "expected_task_version": 1,
            "expected_progress_version": 0,
            "reported_percent": "99",
            "remaining_hours": None,
            "spent_hours": "3",
            "reporting_date": "2026-09-29",
            "done_text": "Prepared the materials",
            "next_steps": "Review",
            **changes,
        }
    )


def test_evidence_required_only_at_reported_completion() -> None:
    validate_items((item(),), datetime(2026, 9, 29, tzinfo=UTC), "UTC")
    with pytest.raises(DailyUpdateError, match="EVIDENCE_REQUIRED"):
        validate_items((item(reported_percent="100"),), datetime(2026, 9, 29, tzinfo=UTC), "UTC")


def test_future_date_timezone_and_daily_limit() -> None:
    at = datetime(2026, 9, 28, 18, tzinfo=UTC)
    validate_items((item(),), at, "Asia/Ho_Chi_Minh")
    with pytest.raises(DailyUpdateError, match="FUTURE_REPORT"):
        validate_items((item(),), at, "UTC")
    with pytest.raises(DailyUpdateError, match="DAILY_HOURS_LIMIT"):
        validate_items((item(spent_hours="15"), item(spent_hours="10")), at, "Asia/Ho_Chi_Minh")
    assert item().spent_hours == Decimal("3")


def test_duplicate_task_and_correction_reason() -> None:
    first = item()
    with pytest.raises(DailyUpdateError, match="DUPLICATE_TASK"):
        validate_items((first, first), datetime(2026, 9, 29, tzinfo=UTC), "UTC")
    with pytest.raises(DailyUpdateError, match="CORRECTION_REASON_REQUIRED"):
        validate_items(
            (item(corrects_observation_id=str(uuid4())),), datetime(2026, 9, 29, tzinfo=UTC), "UTC"
        )
