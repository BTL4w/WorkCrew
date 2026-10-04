"""Local calendar reporting periods, independent of business-state timestamps."""

from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict


class ReportKind(StrEnum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"


class ReportPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: ReportKind
    local_start: date
    local_end: date
    timezone: str
    start_utc: datetime
    end_utc: datetime
    observed_through: datetime
    partial_period: bool


def normalize_period(
    kind: ReportKind, local_start: date, timezone: str, captured_at: datetime
) -> ReportPeriod:
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("capture must be timezone aware")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("invalid reporting timezone") from exc
    if kind == ReportKind.WEEKLY and local_start.weekday() != 0:
        raise ValueError("weekly period must start on Monday")
    at = captured_at.astimezone(UTC)
    try:
        starts = datetime.combine(local_start, time.min, zone).astimezone(UTC)
        if starts > at:
            raise ValueError("future report period")
        local_end = local_start + timedelta(days=7 if kind == ReportKind.WEEKLY else 1)
        ends = datetime.combine(local_end, time.min, zone).astimezone(UTC)
    except OverflowError as exc:
        raise ValueError("unsupported reporting date") from exc
    return ReportPeriod(
        kind=kind,
        local_start=local_start,
        local_end=local_end,
        timezone=timezone,
        start_utc=starts,
        end_utc=ends,
        observed_through=min(at, ends),
        partial_period=at < ends,
    )
