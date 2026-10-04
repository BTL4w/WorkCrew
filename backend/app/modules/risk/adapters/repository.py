"""Authoritative context, RLS transactions, jobs and append-only risk facts."""

import hashlib
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.audit.domain.events import AuditOutcome
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.people_capacity.adapters.database_models import CapacityEntryModel, LeaveEntryModel
from app.modules.people_capacity.application.workload_service import (
    RemainingWorkInput,
    remaining_work,
)
from app.modules.progress.adapters.assessment_models import WarningAcknowledgmentModel
from app.modules.progress.adapters.blocker_models import BlockerModel
from app.modules.progress.adapters.blocker_repository import SqlAlchemyBlockerRepository
from app.modules.progress.adapters.daily_update_models import (
    TaskActualProjectionModel,
    TaskProgressObservationModel,
)
from app.modules.progress.adapters.progress_models import WeeklyPlanBaselineModel
from app.modules.progress.adapters.progress_repository import plan_from_model
from app.modules.progress.domain.blockers import BlockerError
from app.modules.progress.domain.weekly_progress import planned_percent
from app.modules.risk.adapters.database_models import (
    RiskAssessmentModel,
    RiskJobModel,
    RiskNotificationModel,
    RiskReviewModel,
)
from app.modules.risk.application.ports import RiskLease
from app.modules.risk.domain.assessments import (
    RiskAssessment,
    RiskFact,
    RiskInputs,
    RiskReviewCommand,
    RiskReviewEvent,
    WeeklyRisk,
    weekly_risk,
)
from app.modules.risk.domain.notifications import RiskNotification, notification_state
from app.modules.risk.domain.read_context import ReadObservation, RiskReadContext
from app.modules.work.adapters.database_models import TaskModel
from app.modules.work.domain.tasks import TaskStatus
from app.modules.work.planning.adapters.database_models import ProjectWeekModel, TaskDependencyModel


def input_hash(inputs: RiskInputs) -> str:
    return hashlib.sha256(
        json.dumps(inputs.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()


class RiskRepository(SqlAlchemyBlockerRepository):
    def __init__(self, session: AsyncSession, actor: AuthenticatedActor, timezone: str = "UTC"):
        super().__init__(session, actor)
        self.timezone = timezone

    async def authenticate(self) -> None:
        await super().authenticate()
        manager = await self.session.scalar(
            text(
                "SELECT EXISTS(SELECT 1 FROM memberships WHERE organization_id=:org "
                "AND id=:member AND role IN ('MANAGER','ADMIN'))"
            ),
            {"org": self.org, "member": self.actor.membership_id},
        )
        if not manager:
            raise BlockerError("FORBIDDEN", 403)

    async def read_context(self, task_id: UUID) -> RiskReadContext:
        # Re-read DB membership; stale AuthenticatedActor.role never grants scope.
        await super().authenticate()
        task = await self.permitted_task(task_id)
        role = await self.session.scalar(
            text("SELECT role FROM memberships WHERE organization_id=:org AND id=:member"),
            {"org": self.org, "member": self.actor.membership_id},
        )
        manager = role in {"ADMIN", "MANAGER"}
        result = None
        if manager:
            result = await self.current(task_id)
            inputs = await self.inputs(task_id)
            facts = inputs.facts
        else:
            facts_list = [
                RiskFact(
                    id=f"task:{task.id}:v{task.version}",
                    kind="TASK",
                    values={
                        "title": task.title,
                        "status": str(task.status),
                        "due_date": task.due_date.isoformat() if task.due_date else None,
                    },
                )
            ]
            observation = await self.effective_observation(task_id)
            if observation and observation.owner_membership_id == self.actor.membership_id:
                facts_list.append(
                    RiskFact(
                        id=f"progress:{observation.id}",
                        kind="PROGRESS",
                        values={
                            "reported_percent": str(observation.reported_percent),
                            "remaining_hours": str(observation.remaining_hours)
                            if observation.remaining_hours is not None
                            else None,
                            "reported_at": observation.reporting_at.isoformat(),
                        },
                    )
                )
            blockers = (
                await self.session.scalars(
                    select(BlockerModel)
                    .where(
                        BlockerModel.organization_id == self.org,
                        BlockerModel.task_id == task_id,
                        BlockerModel.archived.is_(False),
                        BlockerModel.status != "RESOLVED",
                    )
                    .order_by(BlockerModel.id)
                    .limit(21)
                )
            ).all()
            if len(blockers) > 20:
                raise BlockerError("RISK_CONTEXT_LIMIT", 422)
            for blocker in blockers:
                facts_list.append(
                    RiskFact(
                        id=f"blocker:{blocker.id}:v{blocker.version}",
                        kind="BLOCKER",
                        values={
                            "text": blocker.payload["text"],
                            "severity": blocker.severity,
                            "status": blocker.status,
                        },
                    )
                )
            warnings = (
                await self.session.scalars(
                    select(WarningAcknowledgmentModel)
                    .join(
                        TaskProgressObservationModel,
                        (
                            TaskProgressObservationModel.organization_id
                            == WarningAcknowledgmentModel.organization_id
                        )
                        & (
                            TaskProgressObservationModel.update_id
                            == WarningAcknowledgmentModel.update_id
                        ),
                    )
                    .where(
                        WarningAcknowledgmentModel.organization_id == self.org,
                        WarningAcknowledgmentModel.owner_membership_id == self.actor.membership_id,
                        TaskProgressObservationModel.task_id == task_id,
                    )
                    .distinct()
                    .order_by(WarningAcknowledgmentModel.acknowledged_at.desc())
                    .limit(10)
                )
            ).all()
            for warning in warnings:
                facts_list.append(
                    RiskFact(
                        id=f"warning:{warning.id}",
                        kind="WARNING",
                        values={
                            "scope": "CONFIRMED_REPORT",
                            "update_id": str(warning.update_id),
                            "acknowledged_at": warning.acknowledged_at.isoformat(),
                            "warnings": warning.payload.get("assessment", {}).get("warnings", []),
                        },
                    )
                )
            facts = tuple(facts_list)
        judgment = result.judgment if result and result.state == "READY" else None
        sources = {f.id for f in facts}
        observations = (
            tuple(
                ReadObservation(id=f"observation:{i}", text=o.text, source_ids=o.source_ids)
                for i, o in enumerate(judgment.observations)
                if set(o.source_ids).issubset(sources)
            )
            if judgment
            else ()
        )
        if not observations:
            observations = tuple(
                ReadObservation(
                    id=f"fact:{fact.id}",
                    text=json.dumps(fact.values, ensure_ascii=False, sort_keys=True),
                    source_ids=(fact.id,),
                )
                for fact in facts[:10]
            )
        state = result.state if result else "UNAVAILABLE"
        value = RiskReadContext(
            task_id=task.id,
            task_version=task.version,
            risk_assessment_id=result.id if result else None,
            fingerprint="",
            state=state,
            score=str(judgment.score) if judgment and judgment.score is not None else None,
            band=result.band if judgment and result else None,
            scope="MANAGER" if manager else "OWN_WORK",
            permitted_sources=facts,
            observations=observations,
            rationale=judgment.rationale if judgment else "",
            limitations=judgment.limitations
            if judgment
            else (
                (result.limitation or "RISK_UNAVAILABLE",)
                if result
                else ("RISK_UNAVAILABLE" if manager else "MANAGER_ASSESSMENT_RESTRICTED",)
            ),
            recommendations=judgment.recommendations if judgment else (),
            affected_week_ids=(task.project_week_id,) if task.project_week_id else (),
        )
        fingerprint = hashlib.sha256(
            json.dumps(value.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        return value.model_copy(update={"fingerprint": fingerprint})

    async def audit(
        self,
        action: str,
        request_id: str,
        key: str | None,
        resource_id: UUID | None,
        code: str | None = None,
    ) -> None:
        self.session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id,
                action=action,
                outcome=AuditOutcome.REJECTED if code else AuditOutcome.SUCCEEDED,
                resource_type="risk",
                resource_id=resource_id,
                request_id=request_id,
                idempotency_key=key,
                before_data={},
                after_data={},
                reason_data={"code": code} if code else {},
            )
        )

    async def effective_observation(self, task_id: UUID) -> TaskProgressObservationModel | None:
        return await self.session.scalar(
            select(TaskProgressObservationModel)
            .join(
                TaskActualProjectionModel,
                (
                    TaskActualProjectionModel.organization_id
                    == TaskProgressObservationModel.organization_id
                )
                & (TaskActualProjectionModel.observation_id == TaskProgressObservationModel.id),
            )
            .where(
                TaskActualProjectionModel.organization_id == self.org,
                TaskActualProjectionModel.task_id == task_id,
            )
        )

    async def load_remaining_work(
        self, *, actor: AuthenticatedActor, membership_id: UUID, observed_on: date
    ) -> RemainingWorkInput:
        if actor.membership_id != self.actor.membership_id or actor.organization_id != self.org:
            raise BlockerError("FORBIDDEN", 403)
        start = observed_on - timedelta(days=observed_on.weekday())
        end = start + timedelta(days=6)
        capacity = (
            await self.session.scalars(
                select(CapacityEntryModel).where(
                    CapacityEntryModel.organization_id == self.org,
                    CapacityEntryModel.membership_id == membership_id,
                    CapacityEntryModel.effective_from <= end,
                    CapacityEntryModel.effective_to >= start,
                )
            )
        ).all()
        override = next(
            (c for c in capacity if str(c.kind) == "OVERRIDE" and c.week_start == start), None
        )
        default = next((c for c in capacity if str(c.kind) == "DEFAULT"), None)
        selected = override or default
        leaves = (
            await self.session.scalars(
                select(LeaveEntryModel).where(
                    LeaveEntryModel.organization_id == self.org,
                    LeaveEntryModel.membership_id == membership_id,
                    LeaveEntryModel.start_date <= end,
                    LeaveEntryModel.end_date >= start,
                )
            )
        ).all()
        leave = Decimal(0)
        for entry in leaves:
            days = [
                entry.start_date + timedelta(days=i)
                for i in range((entry.end_date - entry.start_date).days + 1)
                if (entry.start_date + timedelta(days=i)).weekday() < 5
            ]
            if days:
                leave += (
                    Decimal(entry.unavailable_hours)
                    * sum(start <= d <= end for d in days)
                    / len(days)
                )
        tasks = (
            await self.session.scalars(
                select(TaskModel)
                .outerjoin(
                    ProjectWeekModel,
                    (ProjectWeekModel.organization_id == TaskModel.organization_id)
                    & (ProjectWeekModel.id == TaskModel.project_week_id),
                )
                .where(
                    TaskModel.organization_id == self.org,
                    TaskModel.assignee_membership_id == membership_id,
                    TaskModel.status != TaskStatus.DONE,
                    or_(
                        (ProjectWeekModel.start_date <= end) & (ProjectWeekModel.end_date >= start),
                        TaskModel.due_date <= end,
                        TaskModel.project_week_id.is_(None),
                    ),
                )
            )
        ).all()
        efforts: list[Decimal | None] = []
        for task in tasks:
            obs = await self.effective_observation(task.id)
            # Reassignment makes previous-owner estimates stale.
            efforts.append(
                obs.remaining_hours if obs and obs.owner_membership_id == membership_id else None
            )
        remaining_days = sum(
            (observed_on + timedelta(days=i)).weekday() < 5
            for i in range((end - observed_on).days + 1)
        )
        return RemainingWorkInput(
            Decimal(selected.hours) if selected else None, leave, remaining_days, tuple(efforts)
        )

    async def inputs(self, task_id: UUID) -> RiskInputs:
        task = await self.permitted_task(task_id)
        today = datetime.now(ZoneInfo(self.timezone)).date()
        facts = [
            RiskFact(
                id=f"task:{task.id}:v{task.version}",
                kind="TASK",
                values={
                    "title": task.title,
                    "estimated_effort_hours": task.estimated_effort_hours,
                    "status": str(task.status),
                    "due_date": task.due_date.isoformat() if task.due_date else None,
                    "overdue": bool(
                        task.due_date and task.due_date < today and task.status != TaskStatus.DONE
                    ),
                    "evaluation_date": today.isoformat(),
                    "timezone": self.timezone,
                    "assignee": str(task.assignee_membership_id)
                    if task.assignee_membership_id
                    else None,
                },
            )
        ]
        missing: list[str] = []
        observation = await self.effective_observation(task_id)
        valid = observation and observation.owner_membership_id == task.assignee_membership_id
        if valid and observation:
            days = sum(
                (observation.reporting_date + timedelta(days=i)).weekday() < 5
                for i in range(1, max(0, (today - observation.reporting_date).days) + 1)
            )
            facts.append(
                RiskFact(
                    id=f"progress:{observation.id}",
                    kind="PROGRESS",
                    values={
                        "reported_percent": str(observation.reported_percent),
                        "remaining_hours": str(observation.remaining_hours)
                        if observation.remaining_hours is not None
                        else None,
                        "reported_at": observation.reporting_at.isoformat(),
                        "working_days_since_report": days,
                    },
                )
            )
        else:
            missing.append("PROGRESS")
        blockers = (
            await self.session.scalars(
                select(BlockerModel)
                .where(
                    BlockerModel.organization_id == self.org,
                    BlockerModel.task_id == task_id,
                    BlockerModel.archived.is_(False),
                    BlockerModel.status != "RESOLVED",
                )
                .order_by(BlockerModel.id)
            )
        ).all()
        for blocker in blockers:
            age = sum(
                (blocker.created_at.date() + timedelta(days=i)).weekday() < 5
                for i in range(1, max(0, (today - blocker.created_at.date()).days) + 1)
            )
            facts.append(
                RiskFact(
                    id=f"blocker:{blocker.id}:v{blocker.version}",
                    kind="BLOCKER",
                    values={
                        "text": blocker.payload["text"],
                        "severity": blocker.severity,
                        "status": blocker.status,
                        "working_days_open": age,
                    },
                )
            )
        edges = (
            await self.session.scalars(
                select(TaskDependencyModel)
                .where(
                    TaskDependencyModel.organization_id == self.org,
                    TaskDependencyModel.successor_task_id == task_id,
                )
                .order_by(TaskDependencyModel.id)
            )
        ).all()
        for edge in edges:
            predecessor = await self.permitted_task(edge.predecessor_task_id)
            severe = await self.session.scalar(
                select(BlockerModel.id)
                .where(
                    BlockerModel.organization_id == self.org,
                    BlockerModel.task_id == predecessor.id,
                    BlockerModel.status != "RESOLVED",
                    BlockerModel.archived.is_(False),
                    BlockerModel.severity.in_(["HIGH", "CRITICAL"]),
                )
                .limit(1)
            )
            facts.append(
                RiskFact(
                    id=f"dependency:{edge.id}:v{edge.version}",
                    kind="DEPENDENCY",
                    values={
                        "predecessor_task_id": str(predecessor.id),
                        "title": predecessor.title,
                        "status": str(predecessor.status),
                        "version": predecessor.version,
                        "overdue": bool(
                            predecessor.due_date
                            and predecessor.due_date < today
                            and predecessor.status != TaskStatus.DONE
                        ),
                        "severe_blocker": severe is not None,
                    },
                )
            )
        if task.assignee_membership_id:
            raw = await self.load_remaining_work(
                actor=self.actor, membership_id=task.assignee_membership_id, observed_on=today
            )
            capacity = remaining_work(raw)
            facts.append(
                RiskFact(
                    id=f"capacity:{task.assignee_membership_id}:{today}",
                    kind="CAPACITY",
                    values={
                        "available_hours": str(capacity.available_hours)
                        if capacity.available_hours is not None
                        else None,
                        "known_remaining_hours": str(capacity.known_hours),
                        "unknown_tasks": capacity.unknown_count,
                        "overload": capacity.overload,
                        "remaining_working_days": raw.remaining_days,
                        "approximation": (
                            "weekly capacity after leave prorated over remaining Mon-Fri days"
                        ),
                        "scope": "all open assignments across Projects",
                    },
                )
            )
            if capacity.available_hours is None or capacity.unknown_count:
                missing.append("CAPACITY")
        else:
            missing.append("ASSIGNEE")
        if task.project_week_id:
            current_week = await self.session.scalar(
                select(ProjectWeekModel).where(
                    ProjectWeekModel.organization_id == self.org,
                    ProjectWeekModel.id == task.project_week_id,
                )
            )
            if current_week:
                facts.append(
                    RiskFact(
                        id=f"week:{current_week.id}:v{current_week.version}",
                        kind="BASELINE",
                        values={
                            "current_week_start": current_week.start_date.isoformat(),
                            "current_week_end": current_week.end_date.isoformat(),
                            "current_week_status": str(current_week.status),
                        },
                    )
                )
            baseline = await self.session.scalar(
                select(WeeklyPlanBaselineModel)
                .where(
                    WeeklyPlanBaselineModel.organization_id == self.org,
                    WeeklyPlanBaselineModel.project_week_id == task.project_week_id,
                )
                .order_by(WeeklyPlanBaselineModel.sequence)
                .limit(1)
            )
            if baseline:
                plan = plan_from_model(baseline)
                entry = next((e for e in plan.task_entries if e.task_id == task_id), None)
                if entry:
                    planned = planned_percent(
                        entry, plan, datetime.combine(today, datetime.min.time(), UTC)
                    )
                    facts.append(
                        RiskFact(
                            id=f"baseline:{baseline.id}",
                            kind="BASELINE",
                            values={
                                "planned_percent": str(planned) if planned is not None else None,
                                "reported_percent": str(observation.reported_percent)
                                if valid and observation
                                else None,
                                "deviation_pp": str(planned - observation.reported_percent)
                                if planned is not None and valid and observation
                                else None,
                                "calendar": "Mon-Fri end of reporting date",
                            },
                        )
                    )
                else:
                    missing.append("BASELINE")
            else:
                missing.append("BASELINE")
        else:
            missing.append("BASELINE")
        # An ignored warning remains reviewable even after another report replaces
        # the progress projection. Only confirmed, tenant-scoped acknowledgments enter context.
        acknowledgments = (
            await self.session.scalars(
                select(WarningAcknowledgmentModel)
                .join(
                    TaskProgressObservationModel,
                    (
                        TaskProgressObservationModel.organization_id
                        == WarningAcknowledgmentModel.organization_id
                    )
                    & (
                        TaskProgressObservationModel.update_id
                        == WarningAcknowledgmentModel.update_id
                    ),
                )
                .where(
                    WarningAcknowledgmentModel.organization_id == self.org,
                    TaskProgressObservationModel.task_id == task_id,
                    WarningAcknowledgmentModel.payload["assessment"]["warnings"]
                    != text("'[]'::jsonb"),
                )
                .distinct()
                .order_by(WarningAcknowledgmentModel.acknowledged_at.desc())
                .limit(11)
            )
        ).all()
        if len(acknowledgments) > 10:
            missing.append("WARNING_HISTORY")
        for ack in acknowledgments[:10]:
            if warnings := ack.payload.get("assessment", {}).get("warnings", []):
                facts.append(
                    RiskFact(
                        id=f"warning:{ack.id}",
                        kind="WARNING",
                        values={
                            "assessment_id": str(ack.assessment_id),
                            "update_id": str(ack.update_id),
                            "scope": "CONFIRMED_REPORT",
                            "acknowledged_at": ack.acknowledged_at.isoformat(),
                            "warnings": warnings,
                        },
                    )
                )
        if len(facts) > 100:
            raise BlockerError("RISK_CONTEXT_LIMIT", 422)
        return RiskInputs(
            task_id=task.id, task_version=task.version, facts=tuple(facts), missing=tuple(missing)
        )

    async def enqueue(self, task_id: UUID, cause_id: UUID) -> RiskAssessment:
        await self.authorize(task_id)
        await self.lock(f"risk-cause:{task_id}:{cause_id}")
        try:
            inputs = await self.inputs(task_id)
        except BlockerError as exc:
            if exc.code != "RISK_CONTEXT_LIMIT":
                raise
            task = await self.permitted_task(task_id)
            inputs = RiskInputs(
                task_id=task.id, task_version=task.version, facts=(), missing=("CONTEXT_LIMIT",)
            )
        existing = await self.session.scalar(
            select(RiskJobModel).where(
                RiskJobModel.organization_id == self.org,
                RiskJobModel.task_id == task_id,
                RiskJobModel.cause_id == cause_id,
            )
        )
        if existing:
            stored = await self.session.scalar(
                select(RiskAssessmentModel).where(
                    RiskAssessmentModel.organization_id == self.org,
                    RiskAssessmentModel.id == existing.id,
                )
            )
            if stored:
                return RiskAssessment.model_validate(stored.payload)
            return RiskAssessment(
                id=existing.id,
                task_id=task_id,
                task_version=inputs.task_version,
                state="PENDING",
                evaluated_at=existing.created_at,
            )
        now = datetime.now(UTC)
        job = RiskJobModel(
            id=uuid4(),
            organization_id=self.org,
            task_id=task_id,
            actor_membership_id=self.actor.membership_id,
            cause_id=cause_id,
            input_hash=input_hash(inputs),
            state="PENDING",
            attempts=0,
            created_at=now,
            deadline=now + timedelta(minutes=10),
            lease_until=None,
        )
        self.session.add(job)
        await self.audit("risk.refresh_requested", "risk", str(cause_id), task_id)
        return RiskAssessment(
            id=job.id,
            task_id=task_id,
            task_version=inputs.task_version,
            state="PENDING",
            evaluated_at=now,
        )

    async def current(self, task_id: UUID) -> RiskAssessment | None:
        limited = False
        try:
            inputs = await self.inputs(task_id)
        except BlockerError as exc:
            if exc.code != "RISK_CONTEXT_LIMIT":
                raise
            task = await self.permitted_task(task_id)
            inputs = RiskInputs(task_id=task.id, task_version=task.version, facts=())
            limited = True
        pending = await self.session.scalar(
            select(RiskJobModel)
            .where(
                RiskJobModel.organization_id == self.org,
                RiskJobModel.task_id == task_id,
                RiskJobModel.state.in_(["PENDING", "RUNNING"]),
            )
            .order_by(RiskJobModel.created_at.desc())
            .limit(1)
        )
        if pending and pending.deadline > datetime.now(UTC):
            return RiskAssessment(
                id=pending.id,
                task_id=task_id,
                task_version=inputs.task_version,
                state="PENDING",
                evaluated_at=pending.created_at,
            )
        row = await self.session.scalar(
            select(RiskAssessmentModel)
            .where(
                RiskAssessmentModel.organization_id == self.org,
                RiskAssessmentModel.task_id == task_id,
            )
            .order_by(RiskAssessmentModel.created_at.desc(), RiskAssessmentModel.id)
            .limit(1)
        )
        if not row:
            return None
        result = RiskAssessment.model_validate(row.payload)
        if limited:
            return result.model_copy(
                update={
                    "state": "UNAVAILABLE",
                    "judgment": None,
                    "band": None,
                    "input_snapshot": None,
                    "limitation": "RISK_CONTEXT_LIMIT",
                }
            )
        if row.input_hash != input_hash(inputs):
            return result.model_copy(
                update={
                    "state": "STALE",
                    "judgment": None,
                    "band": None,
                    "limitation": "CONTEXT_CHANGED",
                }
            )
        return result

    async def weekly(self, week_id: UUID) -> WeeklyRisk:
        week = await self.session.scalar(
            select(ProjectWeekModel).where(
                ProjectWeekModel.organization_id == self.org, ProjectWeekModel.id == week_id
            )
        )
        if week is None:
            raise BlockerError("RESOURCE_NOT_FOUND", 404)
        tasks = tuple(
            (
                await self.session.scalars(
                    select(TaskModel.id)
                    .where(
                        TaskModel.organization_id == self.org, TaskModel.project_week_id == week_id
                    )
                    .order_by(TaskModel.id)
                    .limit(101)
                )
            ).all()
        )
        if len(tasks) > 100:
            raise BlockerError("RISK_CONTEXT_LIMIT", 422)
        results: list[RiskAssessment] = []
        for task in tasks:
            result = await self.current(task)
            if result:
                results.append(result)
        return weekly_risk(week_id, tasks, tuple(results))

    async def assessment(self, risk_id: UUID) -> RiskAssessmentModel:
        row = await self.session.scalar(
            select(RiskAssessmentModel).where(
                RiskAssessmentModel.organization_id == self.org, RiskAssessmentModel.id == risk_id
            )
        )
        if row is None:
            raise BlockerError("RESOURCE_NOT_FOUND", 404)
        await self.permitted_task(row.task_id)
        return row

    async def get(self, risk_id: UUID) -> RiskAssessment:
        row = await self.assessment(risk_id)
        return RiskAssessment.model_validate(row.payload)

    async def reviews(self, risk_id: UUID) -> tuple[RiskReviewEvent, ...]:
        await self.assessment(risk_id)
        rows = (
            await self.session.scalars(
                select(RiskReviewModel)
                .where(
                    RiskReviewModel.organization_id == self.org, RiskReviewModel.risk_id == risk_id
                )
                .order_by(RiskReviewModel.created_at, RiskReviewModel.id)
            )
        ).all()
        return tuple(RiskReviewEvent.model_validate(r.payload) for r in rows)

    async def review(self, risk_id: UUID, command: RiskReviewCommand, key: str) -> RiskReviewEvent:
        await self.assessment(risk_id)
        fingerprint = hashlib.sha256(f"{risk_id}:{command.model_dump_json()}".encode()).hexdigest()
        replay = await self.replay("risk.review", key, fingerprint)
        if replay:
            return RiskReviewEvent.model_validate(replay)
        result = RiskReviewEvent(
            **command.model_dump(),
            id=uuid4(),
            risk_id=risk_id,
            actor_membership_id=self.actor.membership_id,
            at=datetime.now(UTC),
        )
        self.session.add(
            RiskReviewModel(
                id=result.id,
                organization_id=self.org,
                risk_id=risk_id,
                actor_membership_id=self.actor.membership_id,
                payload=result.model_dump(mode="json"),
                created_at=result.at,
            )
        )
        await self.remember("risk.review", key, fingerprint, result.model_dump(mode="json"))
        await self.audit("risk.review_recorded", "risk", key, risk_id)
        self.emit("risk.review_recorded.v1", result.id, {"risk_id": str(risk_id)})
        return result

    async def claim(self) -> RiskLease | None:

        now = datetime.now(UTC)
        row = await self.session.scalar(
            select(RiskJobModel)
            .where(
                RiskJobModel.organization_id == self.org,
                or_(
                    RiskJobModel.state == "PENDING",
                    (RiskJobModel.state == "RUNNING") & (RiskJobModel.lease_until < now),
                ),
            )
            .order_by(RiskJobModel.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return None
        limitation = ""
        inputs = None
        try:
            inputs = await self.inputs(row.task_id)
        except BlockerError as exc:
            if exc.code != "RISK_CONTEXT_LIMIT":
                raise
            limitation = "RISK_CONTEXT_LIMIT"
        task = await self.permitted_task(row.task_id)
        if limitation or row.deadline <= now or row.attempts >= 3:
            result = RiskAssessment(
                id=row.id,
                task_id=row.task_id,
                task_version=task.version,
                state="UNAVAILABLE",
                input_snapshot=inputs,
                evaluated_at=now,
                limitation=limitation or "REFRESH_EXPIRED",
            )
            self.session.add(
                RiskAssessmentModel(
                    id=row.id,
                    organization_id=self.org,
                    task_id=row.task_id,
                    cause_id=row.cause_id,
                    input_hash=input_hash(inputs) if inputs else row.input_hash,
                    payload=result.model_dump(mode="json"),
                    created_at=now,
                )
            )
            row.state = "FAILED"
            await self.audit("risk.assessed", "risk", str(row.cause_id), result.id)
            self.emit(
                "risk.assessed.v1",
                result.id,
                {"risk_id": str(result.id), "task_id": str(result.task_id)},
            )
            return None
        assert inputs is not None
        row.state = "RUNNING"
        row.attempts += 1
        row.lease_until = now + timedelta(seconds=60)
        return RiskLease(row.id, row.task_id, row.cause_id, row.attempts, inputs)

    async def finish(self, lease: RiskLease, result: RiskAssessment) -> None:
        row = await self.session.scalar(
            select(RiskJobModel)
            .where(RiskJobModel.organization_id == self.org, RiskJobModel.id == lease.id)
            .with_for_update()
        )
        if (
            not row
            or row.state != "RUNNING"
            or row.attempts != lease.attempt
            or not row.lease_until
            or row.lease_until <= datetime.now(UTC)
        ):
            raise BlockerError("STALE_RISK_JOB", 409)
        try:
            current = await self.inputs(lease.task_id)
        except BlockerError as exc:
            if exc.code != "RISK_CONTEXT_LIMIT":
                raise
            current = None
        if current is None or input_hash(current) != input_hash(lease.inputs):
            result = result.model_copy(
                update={
                    "state": "STALE" if current else "UNAVAILABLE",
                    "judgment": None,
                    "band": None,
                    "limitation": "CONTEXT_CHANGED" if current else "RISK_CONTEXT_LIMIT",
                }
            )
        self.session.add(
            RiskAssessmentModel(
                id=result.id,
                organization_id=self.org,
                task_id=lease.task_id,
                cause_id=lease.cause_id,
                input_hash=input_hash(lease.inputs),
                payload=result.model_dump(mode="json"),
                created_at=result.evaluated_at,
            )
        )
        row.state = "DONE" if result.state == "READY" else "FAILED"
        await self.audit("risk.assessed", "risk", str(lease.cause_id), result.id)
        self.emit(
            "risk.assessed.v1",
            result.id,
            {"risk_id": str(result.id), "task_id": str(result.task_id)},
        )

    def emit(self, event_type: str, aggregate_id: UUID, payload: dict[str, object]) -> None:
        from app.modules.planning_runs.adapters.database_models import OutboxEventModel

        event_id = uuid4()
        self.session.add(
            OutboxEventModel(
                id=event_id,
                organization_id=self.org,
                event_id=event_id,
                event_type=event_type,
                aggregate_type="risk",
                aggregate_id=aggregate_id,
                envelope_version="1.0",
                payload=payload,
            )
        )

    async def notify(self, risk_id: UUID) -> None:
        row = await self.assessment(risk_id)
        result = RiskAssessment.model_validate(row.payload)
        if result.state == "STALE" or not result.input_snapshot:
            return
        try:
            current = await self.inputs(row.task_id)
        except BlockerError as exc:
            if exc.code != "RISK_CONTEXT_LIMIT":
                raise
            return
        if row.input_hash != input_hash(current):
            return
        kinds: list[Literal["HIGH_RISK", "SEVERE_BLOCKER", "EVIDENCE_REVIEW"]] = []
        if result.band == "HIGH":
            kinds.append("HIGH_RISK")
        if any(
            f.kind == "BLOCKER" and f.values.get("severity") in ("HIGH", "CRITICAL")
            for f in result.input_snapshot.facts
        ):
            kinds.append("SEVERE_BLOCKER")
        if any(f.kind == "WARNING" for f in result.input_snapshot.facts):
            kinds.append("EVIDENCE_REVIEW")
        for kind in kinds:
            state = notification_state(result.input_snapshot, result.band)
            key = f"{result.task_id}:{kind}:{state}"
            await self.lock(f"risk-notice:{self.actor.membership_id}:{key}")
            exists = await self.session.scalar(
                select(RiskNotificationModel.id).where(
                    RiskNotificationModel.organization_id == self.org,
                    RiskNotificationModel.recipient_membership_id == self.actor.membership_id,
                    RiskNotificationModel.dedup_key == key,
                )
            )
            if exists:
                continue
            now = datetime.now(UTC)
            notice = RiskNotification(
                id=uuid4(), task_id=result.task_id, risk_id=risk_id, kind=kind, created_at=now
            )
            self.session.add(
                RiskNotificationModel(
                    id=notice.id,
                    organization_id=self.org,
                    task_id=notice.task_id,
                    risk_id=risk_id,
                    recipient_membership_id=self.actor.membership_id,
                    dedup_key=key,
                    payload=notice.model_dump(mode="json"),
                    read=False,
                    created_at=now,
                )
            )
            await self.audit("risk.notification_delivered", "risk", None, notice.id)

    async def notifications(self) -> tuple[RiskNotification, ...]:
        rows = (
            await self.session.scalars(
                select(RiskNotificationModel)
                .where(
                    RiskNotificationModel.organization_id == self.org,
                    RiskNotificationModel.recipient_membership_id == self.actor.membership_id,
                )
                .order_by(RiskNotificationModel.created_at.desc())
                .limit(100)
            )
        ).all()
        return tuple(
            RiskNotification.model_validate(r.payload).model_copy(update={"read": r.read})
            for r in rows
        )

    async def read_notification(self, id: UUID, key: str) -> RiskNotification:
        row = await self.session.scalar(
            select(RiskNotificationModel)
            .where(
                RiskNotificationModel.organization_id == self.org,
                RiskNotificationModel.id == id,
                RiskNotificationModel.recipient_membership_id == self.actor.membership_id,
            )
            .with_for_update()
        )
        if not row:
            raise BlockerError("RESOURCE_NOT_FOUND", 404)
        await self.permitted_task(row.task_id)
        replay = await self.replay("risk.notification_read", key, str(id))
        if replay:
            return RiskNotification.model_validate(replay)
        row.read = True
        result = RiskNotification.model_validate(row.payload).model_copy(update={"read": True})
        await self.remember("risk.notification_read", key, str(id), result.model_dump(mode="json"))
        await self.audit("risk.notification_read", "risk", key, id)
        self.emit("risk.notification_read.v1", id, {"notification_id": str(id)})
        return result


class RiskTransactions:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], timezone: str = "UTC"):
        self.sessions = sessions
        self.timezone = timezone

    @asynccontextmanager
    async def __call__(self, actor: AuthenticatedActor) -> AsyncGenerator[RiskRepository]:
        async with self.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true),"
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            yield RiskRepository(session, actor, self.timezone)
