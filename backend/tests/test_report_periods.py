"""Local reporting periods preserve DST and distinguish capture from activity time."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.modules.reporting.domain.periods import ReportKind, normalize_period


def test_daily_period_timezone_and_dst() -> None:
    period = normalize_period(
        ReportKind.DAILY,
        date(2026, 10, 4),
        "Asia/Ho_Chi_Minh",
        datetime(2026, 10, 4, 9, tzinfo=UTC),
    )
    assert period.start_utc == datetime(2026, 10, 3, 17, tzinfo=UTC)
    assert period.end_utc == datetime(2026, 10, 4, 17, tzinfo=UTC)
    assert period.observed_through == datetime(2026, 10, 4, 9, tzinfo=UTC)
    assert period.partial_period is True
    for day, hours in [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)]:
        captured = datetime(2026, 11, 1, tzinfo=UTC)
        dst = normalize_period(ReportKind.DAILY, day, "Europe/Berlin", captured)
        assert dst.end_utc - dst.start_utc == timedelta(hours=hours)
        assert dst.partial_period is False
        assert dst.observed_through == dst.end_utc


@pytest.mark.parametrize("timezone", ["Unknown/Zone", "", "+07:00"])
def test_invalid_iana_timezone_rejected(timezone: str) -> None:
    with pytest.raises(ValueError):
        normalize_period(
            ReportKind.DAILY, date(2026, 10, 4), timezone, datetime(2026, 10, 4, 12, tzinfo=UTC)
        )


def test_future_period_and_naive_capture_rejected() -> None:
    with pytest.raises(ValueError):
        normalize_period(
            ReportKind.DAILY, date(2026, 10, 5), "UTC", datetime(2026, 10, 4, 12, tzinfo=UTC)
        )
    with pytest.raises(ValueError):
        normalize_period(ReportKind.DAILY, date(2026, 10, 4), "UTC", datetime(2026, 10, 4, 12))


def test_extreme_calendar_date_is_a_validation_error():
    with pytest.raises(ValueError):
        normalize_period(
            ReportKind.DAILY, date(9999, 12, 31), "UTC", datetime(2026, 10, 4, tzinfo=UTC)
        )
