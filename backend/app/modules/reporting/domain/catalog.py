"""Pure report arithmetic; source selection and permission stay in the SQL adapter."""

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from .metrics import MetricState, MetricValue
from .periods import ReportPeriod


@dataclass(frozen=True, slots=True)
class TaskInput:
    id: UUID
    status: str
    created_at: datetime
    due_date: date | None
    effort_hours: Decimal | None


@dataclass(frozen=True, slots=True)
class ObservationInput:
    id: UUID
    task_id: UUID
    version: int
    reported_percent: Decimal
    remaining_hours: Decimal | None
    confirmed_at: datetime
    reporting_timezone: str
    stale: bool = False


@dataclass(frozen=True, slots=True)
class WorkLogInput:
    id: UUID
    observation_id: UUID
    task_id: UUID
    reporting_date: date
    spent_hours: Decimal
    supersedes_log_id: UUID | None
    reporting_timezone: str


def metric(
    key: str,
    value: Decimal | int | None,
    *,
    unit: str = "COUNT",
    state: MetricState | None = None,
    basis: str = "AT_CAPTURE",
    policy: str = "report-metrics.v1",
    limitations: tuple[str, ...] = (),
) -> MetricValue:
    return MetricValue.model_validate(
        {
            "key": key,
            "value": Decimal(value) if value is not None else None,
            "unit": unit,
            "state": state or (MetricState.KNOWN if value is not None else MetricState.UNKNOWN),
            "time_basis": basis,
            "policy_version": policy,
            "limitations": limitations,
        }
    )


def core_metrics(
    tasks: tuple[TaskInput, ...],
    observations: tuple[ObservationInput, ...],
    work_logs: tuple[WorkLogInput, ...],
    period: ReportPeriod,
    captured_at: datetime,
) -> dict[str, MetricValue]:
    counts = Counter(task.status for task in tasks)
    results = {
        f"tasks.status.{key}_count": metric(f"tasks.status.{key}_count", value)
        for key, value in (
            ("total", len(tasks)),
            ("to_do", counts["TO_DO"]),
            ("in_progress", counts["IN_PROGRESS"]),
            ("done", counts["DONE"]),
        )
    }
    today = captured_at.astimezone(ZoneInfo(period.timezone)).date()
    upcoming_end = today + timedelta(days=7)
    open_tasks = tuple(t for t in tasks if t.status != "DONE")
    for key, value in (
        ("overdue", sum(t.due_date is not None and t.due_date < today for t in open_tasks)),
        (
            "upcoming",
            sum(t.due_date is not None and today <= t.due_date < upcoming_end for t in open_tasks),
        ),
        ("unknown", sum(t.due_date is None for t in tasks)),
    ):
        name = f"tasks.deadline.{key}_count"
        results[name] = metric(name, value, policy="deadline-7-calendar-days.v1")
    name = "tasks.activity.created_count"
    results[name] = metric(
        name,
        sum(period.start_utc <= t.created_at < period.observed_through for t in tasks),
        basis="IN_PERIOD",
    )
    scope_ids = {task.id for task in tasks}
    latest: dict[UUID, ObservationInput] = {}
    for observation in observations:
        if observation.task_id not in scope_ids or observation.confirmed_at > captured_at:
            continue
        old = latest.get(observation.task_id)
        if old is None or observation.version > old.version:
            latest[observation.task_id] = observation
    known = {id: o for id, o in latest.items() if not o.stale}
    no_tasks = MetricState.NOT_APPLICABLE if not tasks else None
    name = "progress.observation_coverage"
    results[name] = metric(
        name,
        Decimal(len(known)) / len(tasks) if tasks else None,
        unit="FRACTION",
        state=no_tasks,
        limitations=("NO_TASKS",) if not tasks else (),
    )
    total_effort = sum(
        (t.effort_hours for t in tasks if t.effort_hours is not None and t.effort_hours > 0),
        Decimal(0),
    )
    known_effort = sum(
        (
            t.effort_hours
            for t in tasks
            if t.id in known and t.effort_hours is not None and t.effort_hours > 0
        ),
        Decimal(0),
    )
    weighted = sum(
        (
            t.effort_hours * known[t.id].reported_percent
            for t in tasks
            if t.id in known and t.effort_hours is not None and t.effort_hours > 0
        ),
        Decimal(0),
    )
    missing_estimates = sum(t.effort_hours is None or t.effort_hours <= 0 for t in tasks)
    for key, value, unit in (
        ("reported_percent", weighted / known_effort if known_effort else None, "PERCENT"),
        ("known_effort_hours", known_effort, "HOURS"),
        ("total_effort_hours", total_effort if not missing_estimates else None, "HOURS"),
        ("estimated_effort_known_subtotal_hours", total_effort, "HOURS"),
        (
            "known_effort_fraction",
            known_effort / total_effort if total_effort and not missing_estimates else None,
            "FRACTION",
        ),
        (
            "missing_estimate_count",
            sum(t.effort_hours is None or t.effort_hours <= 0 for t in tasks),
            "COUNT",
        ),
    ):
        name = f"progress.{key}"
        # The weighted result only describes the observed subset when coverage is incomplete.
        state = (
            MetricState.PARTIAL
            if key == "reported_percent"
            and value is not None
            and (len(known) != len(tasks) or known_effort != total_effort or missing_estimates)
            else None
        )
        results[name] = metric(name, value, unit=unit, state=state)
    remaining = tuple(o.remaining_hours for o in known.values() if o.remaining_hours is not None)
    subtotal = sum(remaining, Decimal(0))
    unknown_count = len(tasks) - len(remaining)
    for key, value, unit in (
        ("known_subtotal_hours", subtotal, "HOURS"),
        ("unknown_count", unknown_count, "COUNT"),
        ("stale_count", sum(o.stale for o in latest.values()), "COUNT"),
        ("total_hours", subtotal if not unknown_count else None, "HOURS"),
    ):
        name = f"remaining.{key}"
        results[name] = metric(name, value, unit=unit)
    superseded = {log.supersedes_log_id for log in work_logs if log.supersedes_log_id is not None}
    effective = tuple(
        log
        for log in work_logs
        if log.task_id in scope_ids
        and log.id not in superseded
        and period.local_start <= log.reporting_date < period.local_end
    )
    name = "work_logs.effective_hours"
    results[name] = metric(
        name,
        sum((log.spent_hours for log in effective), Decimal(0)),
        unit="HOURS",
        basis="DECLARED_REPORTING_DATE",
        limitations=tuple(
            f"SOURCE_TIMEZONE:{zone}"
            for zone in sorted({log.reporting_timezone for log in effective})
        ),
    )
    return results
