"""Catalog arithmetic keeps capture state, declared dates and unknowns distinct."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from app.modules.reporting.domain.catalog import (
    ObservationInput,
    TaskInput,
    WorkLogInput,
    core_metrics,
)
from app.modules.reporting.domain.metrics import MetricState
from app.modules.reporting.domain.periods import ReportKind, normalize_period

AT = datetime(2026, 10, 5, 12, tzinfo=UTC)
PERIOD = normalize_period(ReportKind.WEEKLY, date(2026, 9, 28), "UTC", AT)


def test_remaining_total_unknown_and_worklog_correction():
    tasks = tuple(
        TaskInput(uuid4(), "IN_PROGRESS", AT - timedelta(days=10), None, Decimal(4))
        for _ in range(4)
    )
    observations = (
        ObservationInput(uuid4(), tasks[0].id, 1, Decimal(50), Decimal("6.5"), AT, "UTC"),
        ObservationInput(uuid4(), tasks[1].id, 1, Decimal(20), Decimal(8), AT, "UTC"),
    )
    original = WorkLogInput(
        uuid4(),
        observations[0].id,
        tasks[0].id,
        date(2026, 9, 29),
        Decimal(2),
        None,
        "Asia/Ho_Chi_Minh",
    )
    correction = WorkLogInput(
        uuid4(),
        observations[1].id,
        tasks[0].id,
        date(2026, 9, 29),
        Decimal(3),
        original.id,
        "Asia/Ho_Chi_Minh",
    )
    metrics = core_metrics(tasks, observations, (original, correction), PERIOD, AT)
    assert metrics["remaining.total_hours"].value is None
    assert metrics["remaining.total_hours"].state == MetricState.UNKNOWN
    assert metrics["remaining.known_subtotal_hours"].value == Decimal("14.5")
    assert metrics["remaining.unknown_count"].value == 2
    assert metrics["work_logs.effective_hours"].value == 3
    assert metrics["work_logs.effective_hours"].time_basis == "DECLARED_REPORTING_DATE"
    assert "SOURCE_TIMEZONE:Asia/Ho_Chi_Minh" in metrics["work_logs.effective_hours"].limitations


def test_coverage_denominators_and_deadline_interval():
    dates = (date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 11), date(2026, 10, 12), None)
    tasks = tuple(TaskInput(uuid4(), "IN_PROGRESS", AT, due, None) for due in dates)
    obs = (ObservationInput(uuid4(), tasks[0].id, 1, Decimal(100), None, AT, "UTC"),)
    metrics = core_metrics(tasks, obs, (), PERIOD, AT)
    assert metrics["tasks.deadline.overdue_count"].value == 1
    assert metrics["tasks.deadline.upcoming_count"].value == 2
    assert metrics["tasks.deadline.unknown_count"].value == 1
    assert metrics["tasks.deadline.upcoming_count"].policy_version == "deadline-7-calendar-days.v1"
    assert metrics["progress.observation_coverage"].value == Decimal("0.2")
    assert metrics["tasks.status.done_count"].value == 0


def test_historical_period_labels_current_state():
    task = TaskInput(uuid4(), "DONE", AT, None, Decimal(8))
    metrics = core_metrics((task,), (), (), PERIOD, AT)
    assert metrics["tasks.status.done_count"].value == 1
    assert metrics["tasks.status.done_count"].time_basis == "AT_CAPTURE"
    assert metrics["tasks.activity.created_count"].value == 0
    assert metrics["tasks.activity.created_count"].time_basis == "IN_PERIOD"
    assert metrics["progress.reported_percent"].value is None


def test_empty_and_stale_observations_are_not_complete():
    empty = core_metrics((), (), (), PERIOD, AT)
    assert empty["progress.observation_coverage"].value is None
    assert empty["progress.observation_coverage"].state == MetricState.NOT_APPLICABLE
    task = TaskInput(uuid4(), "IN_PROGRESS", AT, None, Decimal(2))
    stale = ObservationInput(uuid4(), task.id, 1, Decimal(90), Decimal(1), AT, "UTC", True)
    metrics = core_metrics((task,), (stale,), (), PERIOD, AT)
    assert metrics["progress.observation_coverage"].value == 0
    assert metrics["remaining.total_hours"].value is None
    assert metrics["remaining.stale_count"].value == 1
    assert metrics["progress.reported_percent"].value is None


def test_latest_observation_and_effort_weighting():
    first = TaskInput(uuid4(), "IN_PROGRESS", AT, None, Decimal(2))
    second = TaskInput(uuid4(), "IN_PROGRESS", AT, None, Decimal(6))
    observations = (
        ObservationInput(uuid4(), first.id, 1, Decimal(10), Decimal(2), AT, "UTC"),
        ObservationInput(uuid4(), first.id, 2, Decimal(100), Decimal(0), AT, "UTC"),
        ObservationInput(uuid4(), second.id, 1, Decimal(0), Decimal(6), AT, "UTC"),
    )
    metrics = core_metrics((first, second), observations, (), PERIOD, AT)
    assert metrics["progress.reported_percent"].value == Decimal(25)
    assert metrics["progress.known_effort_hours"].value == Decimal(8)
    assert metrics["remaining.total_hours"].value == 6


def test_observation_after_capture_does_not_enter_snapshot():
    task = TaskInput(uuid4(), "IN_PROGRESS", AT, None, Decimal(2))
    future = ObservationInput(
        uuid4(), task.id, 2, Decimal(100), Decimal(0), AT + timedelta(seconds=1), "UTC"
    )
    assert (
        core_metrics((task,), (future,), (), PERIOD, AT)["progress.observation_coverage"].value == 0
    )


def test_missing_estimate_does_not_make_known_total():
    tasks = (
        TaskInput(uuid4(), "IN_PROGRESS", AT, None, Decimal(2)),
        TaskInput(uuid4(), "IN_PROGRESS", AT, None, None),
    )
    observations = tuple(
        ObservationInput(uuid4(), t.id, 1, Decimal(40), Decimal(1), AT, "UTC") for t in tasks
    )
    metrics = core_metrics(tasks, observations, (), PERIOD, AT)
    assert metrics["progress.total_effort_hours"].value is None
    assert metrics["progress.estimated_effort_known_subtotal_hours"].value == 2
    assert metrics["progress.reported_percent"].state == MetricState.PARTIAL
    assert metrics["progress.known_effort_fraction"].value is None
