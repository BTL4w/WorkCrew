"""Pure calendar arithmetic. Window identity excludes the mutable schedule version."""

from datetime import UTC, datetime, time, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from .schedules import DailySummarySchedule, ReportingWindow


def valid_instant(wall: datetime, zone: ZoneInfo) -> datetime:
    # Cutoffs have minute precision; choose first fold, or first valid wall minute.
    for offset in range(48 * 60 + 1):
        candidate = (wall + timedelta(minutes=offset)).replace(tzinfo=zone, fold=0)
        utc = candidate.astimezone(UTC)
        if utc.astimezone(zone).replace(tzinfo=None) == candidate.replace(tzinfo=None):
            return utc
    raise ValueError("UNRESOLVABLE_LOCAL_TIME")


def resolve_window(
    schedule: DailySummarySchedule,
    at: datetime,
    expected_reporters: tuple[UUID, ...] = (),
) -> ReportingWindow:
    if at.tzinfo is None:
        raise ValueError("AWARE_DATETIME_REQUIRED")
    zone = ZoneInfo(schedule.timezone)
    day = at.astimezone(zone).date()
    start = valid_instant(datetime.combine(day, time.min), zone)
    end = valid_instant(datetime.combine(day + timedelta(days=1), time.min), zone)
    if schedule.effective_at is not None:
        start = max(start, schedule.effective_at)
    cutoff = max(
        start, valid_instant(datetime.combine(day, time.fromisoformat(schedule.cutoff)), zone)
    )
    return ReportingWindow(
        id=uuid5(NAMESPACE_URL, f"daily-summary:{schedule.id}:{start.isoformat()}"),
        schedule_id=schedule.id,
        applied_version=schedule.version,
        local_date=day.isoformat(),
        timezone=schedule.timezone,
        starts_at=start,
        cutoff_at=cutoff,
        ends_at=end,
        expected_reporters=expected_reporters,
        coverage_state="PARTIAL" if expected_reporters else "NO_REPORTERS",
        enabled=day.isoweekday() in schedule.weekdays and not schedule.paused,
    )
