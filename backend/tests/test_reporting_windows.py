"""Calendar boundaries are deterministic, including DST and empty reporter sets."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.modules.automations.domain.schedules import DailySummarySchedule, ScheduleCommand
from app.modules.automations.domain.windows import resolve_window


def schedule(zone: str = "America/New_York", cutoff: str = "01:30"):
    return DailySummarySchedule(
        id=uuid4(),
        version=1,
        paused=False,
        **ScheduleCommand(
            project_id=uuid4(),
            timezone=zone,
            weekdays=(1, 2, 3, 4, 5, 6, 7),
            cutoff=cutoff,
            recipients=(uuid4(),),
        ).model_dump(),
    )


def test_empty_roster_and_schedule_edit_preserve_window():
    original = schedule()
    at = datetime(2026, 11, 1, 12, tzinfo=UTC)
    window = resolve_window(original, at)
    edited = original.model_copy(update={"version": 2})
    after = resolve_window(edited, at)
    next_window = resolve_window(edited, datetime(2026, 11, 2, 12, tzinfo=UTC))
    assert window.coverage_state == "NO_REPORTERS"
    assert window.id == after.id
    assert next_window.applied_version == 2
    assert not window.full_coverage


def test_ambiguous_cutoff_uses_first_occurrence():
    window = resolve_window(schedule(), datetime(2026, 11, 1, 12, tzinfo=UTC))
    assert window.cutoff_at == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert (window.ends_at - window.starts_at).total_seconds() == 25 * 3600


def test_nonexistent_cutoff_uses_next_valid_instant():
    window = resolve_window(schedule(cutoff="02:30"), datetime(2026, 3, 8, 12, tzinfo=UTC))
    assert window.cutoff_at == datetime(2026, 3, 8, 7, tzinfo=UTC)
    assert (window.ends_at - window.starts_at).total_seconds() == 23 * 3600


@pytest.mark.parametrize(
    "values",
    [
        {"timezone": "Not/AZone"},
        {"weekdays": ()},
        {"weekdays": (1, 1)},
        {"cutoff": "25:00"},
        {"recipients": ()},
    ],
)
def test_invalid_settings_are_rejected(values: dict[str, object]):
    valid = schedule().model_dump(exclude={"id", "version", "paused", "effective_at"})
    ScheduleCommand.model_validate(valid)
    with pytest.raises(ValueError):
        ScheduleCommand.model_validate({**valid, **values})
