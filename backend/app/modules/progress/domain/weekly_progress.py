"""Immutable planning references and pure, Decimal-based weekly aggregation."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

CALENDAR_VERSION = "mon-fri-utc-v1"


@dataclass(frozen=True, slots=True)
class PlanEntry:
    task_id: UUID
    task_version: int
    title: str
    effort_hours: Decimal | None
    due_date: date | None
    predecessor_ids: tuple[UUID, ...]
    captured_status: str = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class PlanBaseline:
    id: UUID
    project_week_id: UUID
    task_entries: tuple[PlanEntry, ...]
    captured_at: datetime
    kind: str
    start_date: date
    end_date: date
    week_version: int
    sealed_at: datetime | None


@dataclass(frozen=True, slots=True)
class TaskActual:
    task_id: UUID
    progress_version: int
    reported_percent: Decimal | None
    remaining_hours: Decimal | None
    observation_id: UUID | None
    reporting_at: datetime | None
    status: str
    stale: bool


@dataclass(frozen=True, slots=True)
class WeekActuals:
    baseline: PlanBaseline
    evaluated_at: datetime
    calendar_version: str
    reported_percent: Decimal | None
    planned_percent: Decimal | None
    known_effort_fraction: Decimal | None
    known_effort_hours: Decimal
    total_effort_hours: Decimal
    task_coverage_fraction: Decimal | None
    unknown_progress_ids: tuple[UUID, ...]
    missing_estimate_ids: tuple[UUID, ...]
    stale_task_ids: tuple[UUID, ...]
    remaining_hours: Decimal | None
    unknown_remaining_ids: tuple[UUID, ...]
    task_actuals: tuple[TaskActual, ...]


def planned_percent(entry: PlanEntry, plan: PlanBaseline, at: datetime) -> Decimal | None:
    end = min(plan.end_date, entry.due_date) if entry.due_date else plan.end_date
    days = (
        plan.start_date + timedelta(days=i) for i in range(max(0, (end - plan.start_date).days + 1))
    )
    working_days = tuple(day for day in days if day.weekday() < 5)
    if not working_days:
        return None
    # Daily reference evaluated at the end of the UTC reporting date.
    elapsed = sum(day <= at.date() for day in working_days)
    return Decimal(elapsed) * 100 / Decimal(len(working_days))


def aggregate_week(
    baseline: PlanBaseline, actuals: tuple[TaskActual, ...], at: datetime
) -> WeekActuals:
    if at.tzinfo is None:
        raise ValueError("timezone-aware evaluation instant required")
    at = at.astimezone(UTC)
    by_id = {item.task_id: item for item in actuals}
    total = known = weighted = planned_weight = planned_total = Decimal(0)
    remaining = Decimal(0)
    unknown: list[UUID] = []
    missing: list[UUID] = []
    stale: list[UUID] = []
    unknown_remaining: list[UUID] = []
    projected: list[TaskActual] = []
    known_count = 0
    for entry in baseline.task_entries:
        item = by_id.get(entry.task_id) or TaskActual(
            entry.task_id, 0, None, None, None, None, "UNKNOWN", False
        )
        projected.append(item)
        applicable = item.reporting_at is not None and item.reporting_at <= at
        percent = item.reported_percent if applicable and not item.stale else None
        if percent is not None and (not percent.is_finite() or not 0 <= percent <= 100):
            raise ValueError("invalid reported progress")
        if percent is None:
            unknown.append(entry.task_id)
        else:
            known_count += 1
        if item.stale:
            stale.append(entry.task_id)
        effort = entry.effort_hours
        if effort is None or not effort.is_finite() or effort <= 0:
            missing.append(entry.task_id)
        else:
            total += effort
            if percent is not None:
                known += effort
                weighted += effort * percent
            planned = planned_percent(entry, baseline, at)
            if planned is not None:
                planned_total += effort
                planned_weight += effort * planned
        rest = item.remaining_hours if applicable and not item.stale else None
        if rest is None:
            unknown_remaining.append(entry.task_id)
        else:
            if not rest.is_finite() or rest < 0:
                raise ValueError("invalid remaining effort")
            remaining += rest
    return WeekActuals(
        baseline,
        at,
        CALENDAR_VERSION,
        weighted / known if known else None,
        planned_weight / planned_total if planned_total else None,
        known / total if total else None,
        known,
        total,
        Decimal(known_count) / len(projected) if projected else None,
        tuple(unknown),
        tuple(missing),
        tuple(stale),
        remaining if len(unknown_remaining) < len(projected) else None,
        tuple(unknown_remaining),
        tuple(projected),
    )
