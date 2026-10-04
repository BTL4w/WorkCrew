"""Phase 3/4 projections captured in the report's existing SQL transaction."""

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.audit.domain.events import AuditOutcome
from app.modules.automations.adapters.database_models import (
    ReportingWindowModel,
    ScheduleModel,
)
from app.modules.automations.domain.schedules import ReportingWindow
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.progress.adapters.blocker_models import BlockerModel
from app.modules.progress.adapters.progress_repository import SqlAlchemyProgressRepository
from app.modules.progress.domain.weekly_progress import aggregate_week
from app.modules.risk.adapters.database_models import RiskAssessmentModel, RiskJobModel
from app.modules.risk.adapters.repository import RiskRepository
from app.modules.work.adapters.database_models import TaskModel
from app.modules.work.planning.adapters.database_models import ProjectWeekModel

from ..domain.catalog import metric
from ..domain.commands import CaptureReportCommand
from ..domain.metrics import MetricState, MetricValue, SourceRef
from ..domain.snapshots import canonical_hash
from .window_reader import window_facts
from .workload_reader import capture_workload


@dataclass(frozen=True)
class AuxiliaryCapture:
    metrics: dict[str, MetricValue]
    sources: tuple[SourceRef, ...]


class AuxiliaryReader:
    def __init__(self, session: AsyncSession, actor: AuthenticatedActor, timezone: str):
        self.session, self.actor, self.timezone = session, actor, timezone
        self.org = actor.organization_id

    async def capture(
        self, command: CaptureReportCommand, tasks: tuple[TaskModel, ...]
    ) -> AuxiliaryCapture:
        values: dict[str, MetricValue] = {}
        refs: list[SourceRef] = []
        ids = [task.id for task in tasks]
        blockers = tuple(
            await self.session.scalars(
                select(BlockerModel).where(
                    BlockerModel.organization_id == self.org,
                    BlockerModel.task_id.in_(ids),
                    BlockerModel.created_at <= command.captured_at,
                )
            )
        )
        opened = tuple(b for b in blockers if b.status != "RESOLVED" and not b.archived)
        values["blockers.open_count"] = metric("blockers.open_count", len(opened))
        values["blockers.severe_count"] = metric(
            "blockers.severe_count", sum(b.severity in ("HIGH", "CRITICAL") for b in opened)
        )
        values["blockers.oldest_age_days"] = metric(
            "blockers.oldest_age_days",
            max(
                (
                    Decimal(str((command.captured_at - b.created_at).total_seconds()))
                    / Decimal(86400)
                    for b in opened
                ),
                default=None,
            ),
            unit="DAYS",
            state=MetricState.NOT_APPLICABLE if not opened else None,
        )
        for blocker in blockers:
            facts: dict[str, JsonValue] = {
                "task_id": str(blocker.task_id),
                "status": blocker.status,
                "severity": blocker.severity,
                "text": str(blocker.payload.get("text", "")),
                "created_at": blocker.created_at.isoformat(),
                "archived": blocker.archived,
            }
            refs.append(
                SourceRef(
                    resource_type="BLOCKER",
                    resource_id=blocker.id,
                    version=blocker.version,
                    observed_at=command.captured_at,
                    label=str(blocker.payload.get("text", "")),
                    facts=facts,
                    fingerprint=canonical_hash(facts),
                )
            )
        weeks = tuple(
            await self.session.scalars(
                select(ProjectWeekModel)
                .where(
                    ProjectWeekModel.organization_id == self.org,
                    ProjectWeekModel.project_id == command.project_id,
                    ProjectWeekModel.start_date < command.period.local_end,
                    ProjectWeekModel.end_date >= command.period.local_start,
                )
                .order_by(ProjectWeekModel.start_date, ProjectWeekModel.id)
            )
        )
        progress = SqlAlchemyProgressRepository(self.session, self.actor)
        values["weekly.in_period_week_count"] = metric(
            "weekly.in_period_week_count", len(weeks), basis="IN_PERIOD"
        )
        for week in weeks:
            captured = await progress.get(command.project_id, week.id)
            if captured is None:
                key = f"weekly.{week.id}.baseline_available"
                values[key] = metric(key, None, limitations=("BASELINE_UNAVAILABLE",))
                continue
            original, current, actuals = captured
            original_ids = {e.task_id for e in original.task_entries}
            current_ids = {e.task_id for e in current.task_entries}
            for label, baseline in (("original", original), ("current", current)):
                result = aggregate_week(baseline, actuals, current.sealed_at or command.captured_at)
                facts = {
                    "project_week_id": str(week.id),
                    "week_number": week.week_number,
                    "baseline_kinds": ["original", "current"]
                    if original.id == current.id
                    else [label],
                    "start_date": baseline.start_date.isoformat(),
                    "end_date": baseline.end_date.isoformat(),
                    "calendar_version": result.calendar_version,
                    "evaluated_at": result.evaluated_at.isoformat(),
                    "sealed_at": baseline.sealed_at.isoformat() if baseline.sealed_at else None,
                    "reported_percent": str(result.reported_percent)
                    if result.reported_percent is not None
                    else None,
                    "planned_percent": str(result.planned_percent)
                    if result.planned_percent is not None
                    else None,
                    "known_effort_hours": str(result.known_effort_hours),
                    "total_effort_hours": str(result.total_effort_hours)
                    if not result.missing_estimate_ids
                    else None,
                    "estimated_effort_known_subtotal_hours": str(result.total_effort_hours),
                    "missing_estimate_count": len(result.missing_estimate_ids),
                    "unknown_progress_count": len(result.unknown_progress_ids),
                    "unknown_remaining_count": len(result.unknown_remaining_ids),
                }
                ref = SourceRef(
                    resource_type="WEEKLY_BASELINE",
                    resource_id=baseline.id,
                    version=baseline.week_version,
                    observed_at=command.captured_at,
                    label=week.objective,
                    facts=facts,
                    fingerprint=canonical_hash(facts),
                )
                if not any(
                    r.resource_type == ref.resource_type and r.resource_id == ref.resource_id
                    for r in refs
                ):
                    refs.append(ref)
                for name, value, unit in (
                    ("reported_percent", result.reported_percent, "PERCENT"),
                    ("planned_percent", result.planned_percent, "PERCENT"),
                    ("known_effort_hours", result.known_effort_hours, "HOURS"),
                    (
                        "total_effort_hours",
                        result.total_effort_hours if not result.missing_estimate_ids else None,
                        "HOURS",
                    ),
                    ("estimated_effort_known_subtotal_hours", result.total_effort_hours, "HOURS"),
                    ("missing_estimate_count", len(result.missing_estimate_ids), "COUNT"),
                    ("task_coverage", result.task_coverage_fraction, "FRACTION"),
                ):
                    key = f"weekly.{week.id}.{label}.{name}"
                    entry = metric(
                        key,
                        value,
                        unit=unit,
                        policy=result.calendar_version,
                        state=MetricState.PARTIAL
                        if value is not None
                        and name in ("reported_percent", "planned_percent")
                        and (
                            result.missing_estimate_ids
                            or (name == "reported_percent" and result.unknown_progress_ids)
                        )
                        else None,
                    )
                    values[key] = entry.model_copy(update={"source_refs": (ref,)})
            for label, count in (
                ("added", len(current_ids - original_ids)),
                ("removed", len(original_ids - current_ids)),
            ):
                key = f"weekly.{week.id}.scope.{label}_count"
                values[key] = metric(key, count)
        await self.capture_risks(command, ids, values, refs)
        await self.capture_windows(command, values, refs)
        await self.capture_transitions(command, ids, values, refs)
        workload, workload_refs = await capture_workload(
            self.session, self.actor, command, weeks, tasks
        )
        values.update(workload)
        refs.extend(workload_refs)
        return AuxiliaryCapture(values, tuple(refs))

    async def capture_risks(
        self,
        command: CaptureReportCommand,
        task_ids: list[UUID],
        values: dict[str, MetricValue],
        refs: list[SourceRef],
    ) -> None:
        assessed = set(
            await self.session.scalars(
                select(RiskAssessmentModel.task_id).where(
                    RiskAssessmentModel.organization_id == self.org,
                    RiskAssessmentModel.task_id.in_(task_ids),
                    RiskAssessmentModel.created_at <= command.captured_at,
                )
            )
        )
        pending = set(
            await self.session.scalars(
                select(RiskJobModel.task_id).where(
                    RiskJobModel.organization_id == self.org,
                    RiskJobModel.task_id.in_(task_ids),
                    RiskJobModel.created_at <= command.captured_at,
                    RiskJobModel.state.in_(("PENDING", "RUNNING")),
                )
            )
        )
        candidates = assessed | pending
        counts = {"READY": 0, "PENDING": 0, "STALE": 0, "UNAVAILABLE": 0}
        repo = RiskRepository(self.session, self.actor, self.timezone)
        for task_id in sorted(candidates):
            result = await repo.current(task_id)
            if result is None:
                continue
            counts[result.state] += 1
            facts: dict[str, JsonValue] = {
                "task_id": str(task_id),
                "state": result.state,
                "score": str(result.judgment.score)
                if result.judgment and result.judgment.score is not None
                else None,
                "band": result.band,
                "model_ref": result.model_ref,
                "prompt_version": result.prompt_version,
                "policy_version": result.policy_version,
                "limitation": result.limitation,
                "input_fingerprint": canonical_hash(result.input_snapshot.model_dump(mode="json"))
                if result.input_snapshot
                else None,
                "source_fact_ids": [fact.id for fact in result.input_snapshot.facts]
                if result.input_snapshot
                else [],
                "evaluated_at": result.evaluated_at.isoformat(),
                "rationale": result.judgment.rationale if result.judgment else None,
                "recommendations": list(result.judgment.recommendations) if result.judgment else [],
            }
            ref = SourceRef(
                resource_type="RISK_JOB" if result.state == "PENDING" else "RISK_ASSESSMENT",
                resource_id=result.id,
                version=max(1, result.task_version),
                observed_at=command.captured_at,
                label=str(task_id),
                facts=facts,
                fingerprint=canonical_hash(facts),
            )
            refs.append(ref)
            key = f"risk.{result.id}.score"
            value = result.judgment.score if result.state == "READY" and result.judgment else None
            values[key] = metric(
                key,
                value,
                unit="SCORE",
                state=MetricState.STALE if result.state == "STALE" else None,
            ).model_copy(update={"source_refs": (ref,)})
        for state, count in counts.items():
            key = f"risk.{state.lower()}_count"
            values[key] = metric(key, count)
        values["risk.missing_count"] = metric("risk.missing_count", len(task_ids) - len(candidates))

    async def capture_windows(
        self, command: CaptureReportCommand, values: dict[str, MetricValue], refs: list[SourceRef]
    ) -> None:
        windows = tuple(
            await self.session.scalars(
                select(ReportingWindowModel)
                .join(
                    ScheduleModel,
                    (ScheduleModel.organization_id == ReportingWindowModel.organization_id)
                    & (ScheduleModel.id == ReportingWindowModel.schedule_id),
                )
                .where(
                    ReportingWindowModel.organization_id == self.org,
                    ScheduleModel.project_id == command.project_id,
                    ReportingWindowModel.starts_at >= command.period.start_utc,
                    ReportingWindowModel.starts_at < command.period.observed_through,
                )
            )
        )
        values["reporter.coverage"] = metric(
            "reporter.coverage",
            None,
            unit="FRACTION",
            state=MetricState.NOT_APPLICABLE,
            limitations=("NO_MATCHING_WINDOW",) if not windows else ("PER_WINDOW_COVERAGE_ONLY",),
        )
        for row in windows:
            window = ReportingWindow.model_validate(row.payload)
            facts, observations = await window_facts(
                self.session, self.actor, command.project_id, window, command.captured_at
            )
            expected_count = int(str(facts["expected_count"]))
            reported_count = int(str(facts["reported_count"]))
            key = f"reporter.window.{window.id}.coverage"
            values[key] = metric(
                key,
                Decimal(reported_count) / expected_count if expected_count else None,
                unit="FRACTION",
                state=MetricState.NOT_APPLICABLE if not expected_count else None,
                limitations=("NO_REPORTERS",) if not expected_count else (),
            )
            for o in observations:
                observation_facts: dict[str, JsonValue] = {
                    "task_id": str(o.task_id),
                    "owner_membership_id": str(o.owner_membership_id),
                    "reporting_date": o.reporting_date.isoformat(),
                    "confirmed_at": o.confirmed_at.isoformat(),
                    "reporting_timezone": str(o.payload.get("reporting_timezone", "UTC")),
                    "reported_percent": str(o.reported_percent),
                }
                refs.append(
                    SourceRef(
                        resource_type="OBSERVATION",
                        resource_id=o.id,
                        version=o.progress_version,
                        observed_at=command.captured_at,
                        label=str(o.task_id),
                        facts=observation_facts,
                        fingerprint=canonical_hash(o.payload),
                    )
                )
            ref = SourceRef(
                resource_type="REPORTING_WINDOW",
                resource_id=window.id,
                version=window.applied_version,
                observed_at=command.captured_at,
                label=window.local_date,
                facts=facts,
                fingerprint=canonical_hash(facts),
            )
            refs.append(ref)
            values[key] = values[key].model_copy(update={"source_refs": (ref,)})
            if (
                command.period.kind.value == "DAILY"
                and window.timezone == command.period.timezone
                and window.local_date == command.period.local_start.isoformat()
            ):
                values["reporter.coverage"] = values[key].model_copy(
                    update={"key": "reporter.coverage"}
                )

    async def capture_transitions(
        self,
        command: CaptureReportCommand,
        task_ids: list[UUID],
        values: dict[str, MetricValue],
        refs: list[SourceRef],
    ) -> None:
        events = tuple(
            await self.session.scalars(
                select(AuditEventModel).where(
                    AuditEventModel.organization_id == self.org,
                    AuditEventModel.resource_type == "task",
                    AuditEventModel.resource_id.in_(task_ids),
                    AuditEventModel.outcome == AuditOutcome.SUCCEEDED,
                    AuditEventModel.occurred_at <= command.captured_at,
                    AuditEventModel.action.in_(("task.created", "task.status_changed")),
                )
            )
        )
        created = {event.resource_id for event in events if event.action == "task.created"}
        complete = set(task_ids).issubset(created)
        in_period = tuple(
            event
            for event in events
            if event.action == "task.status_changed"
            and command.period.start_utc <= event.occurred_at < command.period.observed_through
        )
        done = {
            e.resource_id
            for e in in_period
            if e.after_data.get("status") == "DONE" and e.before_data.get("status") != "DONE"
        }
        reopened = {
            e.resource_id
            for e in in_period
            if e.before_data.get("status") == "DONE" and e.after_data.get("status") != "DONE"
        }
        for key, count in (("done_transition_count", len(done)), ("reopened_count", len(reopened))):
            name = f"tasks.activity.{key}"
            values[name] = metric(
                name,
                count if complete else None,
                basis="IN_PERIOD",
                limitations=("CURRENT_TASK_SCOPE",)
                if complete
                else ("INCOMPLETE_TRANSITION_HISTORY",),
            )
        for event in in_period:
            facts: dict[str, JsonValue] = {
                "task_id": str(event.resource_id),
                "occurred_at": event.occurred_at.isoformat(),
                "from_status": str(event.before_data.get("status", "")),
                "to_status": str(event.after_data.get("status", "")),
                "action": event.action,
            }
            refs.append(
                SourceRef(
                    resource_type="TASK_TRANSITION",
                    resource_id=event.id,
                    version=1,
                    observed_at=command.captured_at,
                    label=event.action,
                    facts=facts,
                    fingerprint=canonical_hash(facts),
                )
            )
