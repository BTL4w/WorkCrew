"""Deterministic effort weights, unknowns and working-day references."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from app.modules.progress.domain.weekly_progress import (
    PlanBaseline,
    PlanEntry,
    TaskActual,
    aggregate_week,
)


def baseline(
    efforts: tuple[Decimal | None, ...] = (Decimal("2"), Decimal("6")), due: date | None = None
) -> PlanBaseline:
    return PlanBaseline(
        uuid4(),
        uuid4(),
        tuple(PlanEntry(uuid4(), 1, "Task", effort, due, ()) for effort in efforts),
        datetime(2026, 9, 28, tzinfo=UTC),
        "MANUAL",
        date(2026, 9, 28),
        date(2026, 10, 2),
        1,
        None,
    )


def actual(
    entry: PlanEntry, percent: Decimal | None, remaining: Decimal | None = None, stale: bool = False
) -> TaskActual:
    return TaskActual(
        entry.task_id,
        1,
        percent,
        remaining,
        uuid4(),
        datetime(2026, 9, 28, tzinfo=UTC),
        "IN_PROGRESS",
        stale,
    )


def test_effort_weighted_progress_preserves_unknown_coverage():
    plan = baseline()
    at = datetime(2026, 9, 29, 23, 59, 59, tzinfo=UTC)
    first, second = plan.task_entries
    complete = aggregate_week(
        plan, (actual(first, Decimal("50")), actual(second, Decimal("100"))), at
    )
    partial = aggregate_week(plan, (actual(first, Decimal("50")), actual(second, None)), at)
    assert complete.reported_percent == Decimal("87.5")
    assert partial.reported_percent == Decimal("50")
    assert partial.known_effort_fraction == Decimal("0.25")
    assert partial.unknown_progress_ids == (second.task_id,)
    assert complete.planned_percent == Decimal("40")
    assert partial.known_effort_hours == Decimal("2")
    assert partial.total_effort_hours == Decimal("8")


def test_zero_denominator_and_unknown_remaining_are_not_zero():
    plan = baseline((None, Decimal("0")))
    result = aggregate_week(plan, (), datetime(2026, 9, 29, tzinfo=UTC))
    assert result.reported_percent is None
    assert result.known_effort_fraction is None
    assert result.remaining_hours is None
    assert len(result.missing_estimate_ids) == 2
    assert len(result.unknown_remaining_ids) == 2


def test_stale_and_future_observations_do_not_count_as_current_coverage():
    plan = baseline()
    first, second = plan.task_entries
    result = aggregate_week(
        plan,
        (actual(first, Decimal("50"), stale=True), actual(second, Decimal("100"))),
        datetime(2026, 9, 29, tzinfo=UTC),
    )
    assert result.reported_percent == 100
    assert result.stale_task_ids == (first.task_id,)
    assert result.known_effort_fraction == Decimal("0.75")
    assert (
        aggregate_week(
            plan, (actual(second, Decimal("100")),), datetime(2026, 9, 27, tzinfo=UTC)
        ).reported_percent
        is None
    )


def test_calendar_due_date_and_boundaries():
    plan = baseline((Decimal("2"),), date(2026, 9, 29))
    assert (
        aggregate_week(plan, (), datetime(2026, 9, 29, 23, 59, tzinfo=UTC)).planned_percent == 100
    )
    assert aggregate_week(plan, (), datetime(2026, 9, 27, tzinfo=UTC)).planned_percent == 0
    assert aggregate_week(plan, (), datetime(2026, 10, 3, tzinfo=UTC)).planned_percent == 100
    weekend = PlanBaseline(
        uuid4(),
        uuid4(),
        plan.task_entries,
        plan.captured_at,
        "MANUAL",
        date(2026, 10, 3),
        date(2026, 10, 4),
        1,
        None,
    )
    assert aggregate_week(weekend, (), datetime(2026, 10, 3, tzinfo=UTC)).planned_percent is None
