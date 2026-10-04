"""Capture bounded relational facts; filter recipient snapshots against current authority."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.identity.adapters.database_models import UserModel
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.adapters.database_models import MembershipModel
from app.modules.planning_runs.adapters.database_models import OutboxEventModel
from app.modules.progress.adapters.assessment_models import WarningAcknowledgmentModel
from app.modules.progress.adapters.blocker_models import BlockerModel
from app.modules.progress.adapters.daily_update_models import (
    DailyUpdateEvidenceLinkModel,
    TaskProgressObservationModel,
)
from app.modules.progress.adapters.daily_update_repository import SqlAlchemyDailyUpdateRepository
from app.modules.progress.adapters.evidence_models import EvidenceOriginalModel
from app.modules.risk.adapters.database_models import RiskAssessmentModel, RiskReviewModel
from app.modules.work.adapters.database_models import ProjectModel, TaskModel

from ..domain.digests import (
    DailySummarySnapshot,
    SummaryCaptured,
    SummaryDelivered,
    SummaryDelivery,
    SummarySource,
    SummaryTask,
)
from ..domain.schedules import DailySummarySchedule, Reporter, ReportingWindow, ScheduleError
from .database_models import ReportingWindowModel, ScheduleModel
from .delivery_models import SummaryDeliveryModel, SummarySnapshotModel, SummaryTriggerModel
from .repository import ScheduleRepository


class DigestRepository(ScheduleRepository):
    async def authenticate_viewer(self) -> None:
        await SqlAlchemyDailyUpdateRepository.authenticate(self)

    async def schedule_for_window(self, window_id: UUID) -> DailySummarySchedule:
        row = await self.session.scalar(
            select(ReportingWindowModel).where(
                ReportingWindowModel.organization_id == self.org,
                ReportingWindowModel.id == window_id,
            )
        )
        if row is None:
            raise ScheduleError("RESOURCE_NOT_FOUND", 404)
        return await self.by_id(row.schedule_id)

    async def applied(self, schedule: DailySummarySchedule, version: int) -> DailySummarySchedule:
        row = await self.session.scalar(
            select(ScheduleModel).where(
                ScheduleModel.organization_id == self.org, ScheduleModel.id == schedule.id
            )
        )
        assert row is not None
        return await self._version(row, version)

    async def existing(self, window_id: UUID) -> DailySummarySnapshot | None:
        row = await self.session.scalar(
            select(SummarySnapshotModel)
            .join(
                SummaryTriggerModel,
                (SummaryTriggerModel.organization_id == SummarySnapshotModel.organization_id)
                & (SummaryTriggerModel.id == SummarySnapshotModel.trigger_id),
            )
            .where(
                SummarySnapshotModel.organization_id == self.org,
                SummaryTriggerModel.window_id == window_id,
            )
        )
        return DailySummarySnapshot.model_validate(row.payload) if row else None

    async def capture(
        self,
        schedule: DailySummarySchedule,
        window: ReportingWindow,
        at: datetime,
        reason: Literal["COVERAGE", "CUTOFF", "RESUME"],
    ) -> DailySummarySnapshot:
        project = await self.session.get(ProjectModel, schedule.project_id)
        assert project is not None
        rows = tuple(
            await self.session.scalars(
                select(TaskModel)
                .where(
                    TaskModel.organization_id == self.org,
                    TaskModel.project_id == schedule.project_id,
                )
                .order_by(TaskModel.id)
                .limit(101)
                .with_for_update()
            )
        )
        unknown: list[str] = ["TASK_LIMIT"] if len(rows) > 100 else []
        tasks: list[SummaryTask] = []
        for row in rows[:100]:
            observation = await self.session.scalar(
                select(TaskProgressObservationModel)
                .where(
                    TaskProgressObservationModel.organization_id == self.org,
                    TaskProgressObservationModel.task_id == row.id,
                    TaskProgressObservationModel.confirmed_at <= at,
                )
                .order_by(TaskProgressObservationModel.progress_version.desc())
                .limit(1)
            )
            if observation is None:
                unknown.append(f"ACTUALS:{row.id}")
            tasks.append(
                SummaryTask(
                    id=row.id,
                    title=row.title,
                    task_version=row.version,
                    status=row.status,
                    assignee_id=row.assignee_membership_id,
                    observation_id=observation.id if observation else None,
                    progress_version=observation.progress_version if observation else 0,
                    reported_percent=str(observation.reported_percent) if observation else None,
                    remaining_hours=str(observation.remaining_hours)
                    if observation and observation.remaining_hours is not None
                    else None,
                    observed_at=observation.confirmed_at if observation else None,
                )
            )
        ids = [t.id for t in tasks]
        sources: list[SummarySource] = []
        blockers = await self.session.scalars(
            select(BlockerModel)
            .where(
                BlockerModel.organization_id == self.org,
                BlockerModel.task_id.in_(ids),
                BlockerModel.status != "RESOLVED",
                BlockerModel.updated_at <= at,
            )
            .order_by(BlockerModel.id)
            .limit(101)
        )
        for blocker in blockers:
            sources.append(
                SummarySource(
                    id=blocker.id,
                    task_id=blocker.task_id,
                    version=blocker.version,
                    kind="BLOCKER",
                    text=str(blocker.payload.get("text", "")),
                    state=blocker.status,
                    created_at=blocker.updated_at,
                )
            )
        risks = tuple(
            await self.session.scalars(
                select(RiskAssessmentModel)
                .where(
                    RiskAssessmentModel.organization_id == self.org,
                    RiskAssessmentModel.task_id.in_(ids),
                    RiskAssessmentModel.created_at <= at,
                )
                .distinct(RiskAssessmentModel.task_id)
                .order_by(
                    RiskAssessmentModel.task_id,
                    RiskAssessmentModel.created_at.desc(),
                    RiskAssessmentModel.id,
                )
                .limit(101)
            )
        )
        seen: set[UUID] = set()
        for risk in risks:
            if risk.task_id in seen:
                continue
            seen.add(risk.task_id)
            sources.append(
                SummarySource(
                    id=risk.id,
                    task_id=risk.task_id,
                    version=1,
                    kind="RISK",
                    state=str(risk.payload.get("state", "UNKNOWN")),
                    created_at=risk.created_at,
                )
            )
        for task_id in set(ids) - seen:
            unknown.append(f"RISK:{task_id}")
        reviews = await self.session.execute(
            select(RiskReviewModel, RiskAssessmentModel.task_id)
            .join(
                RiskAssessmentModel,
                (RiskReviewModel.organization_id == RiskAssessmentModel.organization_id)
                & (RiskReviewModel.risk_id == RiskAssessmentModel.id),
            )
            .where(
                RiskReviewModel.organization_id == self.org,
                RiskAssessmentModel.task_id.in_(ids),
                RiskReviewModel.created_at <= at,
            )
            .order_by(RiskReviewModel.id)
            .limit(101)
        )
        for review, task_id in reviews:
            sources.append(
                SummarySource(
                    id=review.id,
                    task_id=task_id,
                    version=1,
                    kind="REVIEW",
                    created_at=review.created_at,
                )
            )
        warnings = await self.session.execute(
            select(WarningAcknowledgmentModel, TaskProgressObservationModel.task_id)
            .join(
                TaskProgressObservationModel,
                (
                    TaskProgressObservationModel.organization_id
                    == WarningAcknowledgmentModel.organization_id
                )
                & (TaskProgressObservationModel.update_id == WarningAcknowledgmentModel.update_id),
            )
            .where(
                WarningAcknowledgmentModel.organization_id == self.org,
                TaskProgressObservationModel.task_id.in_(ids),
                WarningAcknowledgmentModel.acknowledged_at <= at,
                WarningAcknowledgmentModel.payload["assessment"]["warnings"] != text("'[]'::jsonb"),
            )
            .order_by(WarningAcknowledgmentModel.id, TaskProgressObservationModel.task_id)
            .limit(101)
        )
        for warning, task_id in warnings:
            sources.append(
                SummarySource(
                    id=warning.id,
                    task_id=task_id,
                    version=1,
                    kind="REVIEW",
                    state="EVIDENCE_WARNING_ACKNOWLEDGED",
                    created_at=warning.acknowledged_at,
                )
            )
        links = await self.session.execute(
            select(DailyUpdateEvidenceLinkModel, TaskProgressObservationModel.task_id)
            .join(
                TaskProgressObservationModel,
                (
                    TaskProgressObservationModel.organization_id
                    == DailyUpdateEvidenceLinkModel.organization_id
                )
                & (TaskProgressObservationModel.id == DailyUpdateEvidenceLinkModel.observation_id),
            )
            .where(
                DailyUpdateEvidenceLinkModel.organization_id == self.org,
                TaskProgressObservationModel.id.in_(
                    [t.observation_id for t in tasks if t.observation_id]
                ),
            )
            .order_by(DailyUpdateEvidenceLinkModel.id)
            .limit(101)
        )
        for link, task_id in links:
            sources.append(
                SummarySource(
                    id=link.id,
                    task_id=task_id,
                    version=link.evidence_version,
                    kind="EVIDENCE",
                    evidence_id=link.evidence_id,
                    evidence_version=link.evidence_version,
                    href=f"/api/v1/evidence/{link.evidence_id}/versions/{link.evidence_version}/content",
                )
            )
        if len(sources) > 100:
            unknown.append("SOURCE_LIMIT")
        reporter_rows = await self.session.execute(
            select(MembershipModel.id, UserModel.display_name)
            .join(UserModel, UserModel.id == MembershipModel.user_id)
            .where(
                MembershipModel.organization_id == self.org,
                MembershipModel.id.in_(window.expected_reporters),
            )
        )
        reporters = tuple(Reporter(membership_id=id, name=name) for id, name in reporter_rows)
        return DailySummarySnapshot(
            id=uuid5(NAMESPACE_URL, f"daily-summary:{self.org}:{window.id}"),
            schedule_id=schedule.id,
            project_id=schedule.project_id,
            project_name=project.name,
            window=window,
            reason=reason,
            snapshot_at=at,
            expected_count=len(window.expected_reporters),
            reported_count=len(window.reported_members),
            reporters=reporters,
            missing_reporters=tuple(
                m for m in window.expected_reporters if m not in window.reported_members
            ),
            tasks=tuple(tasks),
            sources=tuple(sources[:100]),
            unknown_inputs=tuple(sorted(unknown)),
        )

    async def create(self, snapshot: DailySummarySnapshot) -> None:
        self.session.add(
            SummaryTriggerModel(
                id=snapshot.id,
                organization_id=self.org,
                schedule_id=snapshot.schedule_id,
                window_id=snapshot.window.id,
                applied_version=snapshot.window.applied_version,
                created_at=snapshot.snapshot_at,
            )
        )
        await self.session.flush()
        self.session.add(
            SummarySnapshotModel(
                id=snapshot.id,
                organization_id=self.org,
                trigger_id=snapshot.id,
                project_id=snapshot.project_id,
                payload=snapshot.model_dump(mode="json"),
                created_at=snapshot.snapshot_at,
            )
        )
        await self.session.flush()
        schedule = await self.by_id(snapshot.schedule_id)
        applied = await self.applied(schedule, snapshot.window.applied_version)
        allowed = {r.membership_id for r in await self.recipients(snapshot.project_id)}
        for recipient in set(applied.recipients) & set(schedule.recipients) & allowed:
            self.session.add(
                SummaryDeliveryModel(
                    id=uuid4(),
                    organization_id=self.org,
                    snapshot_id=snapshot.id,
                    recipient_id=recipient,
                    state="PENDING",
                    attempts=0,
                    worker_id=None,
                    lease_until=None,
                    retry_at=snapshot.snapshot_at,
                    created_at=snapshot.snapshot_at,
                    payload=None,
                )
            )
        self.session.add(
            OutboxEventModel(
                id=uuid4(),
                organization_id=self.org,
                event_id=uuid4(),
                event_type="automation.summary.captured.v1",
                aggregate_type="daily_summary",
                aggregate_id=snapshot.id,
                payload=SummaryCaptured(summary_id=snapshot.id).model_dump(mode="json"),
            )
        )
        await self.record_audit("summary.captured", str(snapshot.id), snapshot.id)

    async def claim(self, at: datetime, worker_id: str) -> UUID | None:
        exhausted = tuple(
            await self.session.scalars(
                select(SummaryDeliveryModel)
                .where(
                    SummaryDeliveryModel.organization_id == self.org,
                    SummaryDeliveryModel.state == "RUNNING",
                    SummaryDeliveryModel.attempts >= 3,
                    SummaryDeliveryModel.lease_until <= at,
                )
                .order_by(SummaryDeliveryModel.id)
                .limit(10)
                .with_for_update(skip_locked=True)
            )
        )
        for failed in exhausted:
            failed.state, failed.worker_id, failed.lease_until = "FAILED", None, None
            await self.record_audit(
                "summary.delivery_failed", str(failed.id), failed.id, "RETRY_EXHAUSTED"
            )
        row = await self.session.scalar(
            select(SummaryDeliveryModel)
            .where(
                SummaryDeliveryModel.organization_id == self.org,
                SummaryDeliveryModel.attempts < 3,
                SummaryDeliveryModel.retry_at <= at,
                or_(
                    SummaryDeliveryModel.state == "PENDING",
                    (SummaryDeliveryModel.state == "RUNNING")
                    & (SummaryDeliveryModel.lease_until <= at),
                ),
            )
            .order_by(SummaryDeliveryModel.retry_at, SummaryDeliveryModel.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return None
        row.state, row.worker_id, row.lease_until = "RUNNING", worker_id, at + timedelta(seconds=60)
        row.attempts += 1
        return row.id

    async def complete_delivery(self, id: UUID, at: datetime, worker_id: str) -> None:
        # Discover the recipient without taking a job lock. All paths lock current
        # authority before delivery rows, preventing claimant/completer inversion.
        recipient = await self.session.scalar(
            select(SummaryDeliveryModel.recipient_id).where(
                SummaryDeliveryModel.organization_id == self.org, SummaryDeliveryModel.id == id
            )
        )
        if recipient is None:
            raise ScheduleError("LEASE_LOST")
        for member_id in sorted({self.actor.membership_id, recipient}):
            await self.session.execute(
                text("SELECT public.lock_active_membership(:org,:member)"),
                {"org": self.org, "member": member_id},
            )
        await self.authenticate()
        row = await self.session.scalar(
            select(SummaryDeliveryModel)
            .where(SummaryDeliveryModel.organization_id == self.org, SummaryDeliveryModel.id == id)
            .with_for_update()
        )
        if (
            row is None
            or row.state != "RUNNING"
            or row.worker_id != worker_id
            or not row.lease_until
            or row.lease_until <= at
        ):
            raise ScheduleError("LEASE_LOST")
        stored = await self.session.get(SummarySnapshotModel, row.snapshot_id)
        assert stored is not None
        snapshot = DailySummarySnapshot.model_validate(stored.payload)
        await self.authorize(snapshot.project_id)
        schedule = await self.by_id(snapshot.schedule_id)
        if at >= snapshot.window.ends_at:
            row.state, row.worker_id, row.lease_until = "CANCELLED", None, None
            await self.record_audit(
                "summary.delivery_cancelled", str(row.id), row.id, "WINDOW_EXPIRED"
            )
            return
        if schedule.paused:
            row.state, row.retry_at = "PENDING", at + timedelta(seconds=30)
            row.attempts -= 1
            return
        member = await self.session.scalar(
            select(MembershipModel)
            .join(UserModel, UserModel.id == MembershipModel.user_id)
            .where(
                MembershipModel.organization_id == self.org,
                MembershipModel.id == row.recipient_id,
                MembershipModel.is_active,
                UserModel.is_active,
            )
        )
        allowed = {r.membership_id for r in await self.recipients(snapshot.project_id)}
        if (
            member is None
            or row.recipient_id not in allowed
            or row.recipient_id not in schedule.recipients
        ):
            row.state, row.worker_id, row.lease_until = "CANCELLED", None, None
            await self.record_audit(
                "summary.delivery_cancelled", str(row.id), row.id, "RECIPIENT_REVOKED"
            )
            return
        row.payload = (await self.project(snapshot, member.id, member.role)).model_dump(mode="json")
        row.state = "DELIVERED"
        row.lease_until = None
        self.session.add(
            OutboxEventModel(
                id=uuid4(),
                organization_id=self.org,
                event_id=uuid4(),
                event_type="automation.summary.delivered.v1",
                aggregate_type="daily_summary_delivery",
                aggregate_id=row.id,
                payload=SummaryDelivered(delivery_id=row.id, recipient_id=member.id).model_dump(
                    mode="json"
                ),
            )
        )
        await self.record_audit("summary.delivered", str(row.id), row.id)

    async def fail_delivery(self, id: UUID, at: datetime, worker_id: str) -> None:
        row = await self.session.scalar(
            select(SummaryDeliveryModel)
            .where(
                SummaryDeliveryModel.organization_id == self.org,
                SummaryDeliveryModel.id == id,
                SummaryDeliveryModel.worker_id == worker_id,
                SummaryDeliveryModel.state == "RUNNING",
            )
            .with_for_update()
        )
        if row and row.lease_until and row.lease_until > at:
            row.state = "FAILED" if row.attempts >= 3 else "PENDING"
            row.retry_at = at + timedelta(seconds=30 * row.attempts)
            row.lease_until = None
            await self.record_audit(
                "summary.delivery_failed", str(row.id), row.id, "DELIVERY_FAILED"
            )

    async def project(
        self, snapshot: DailySummarySnapshot, member_id: UUID, role: str
    ) -> DailySummarySnapshot:
        if role in {"MANAGER", "ADMIN"}:
            return snapshot
        rows = tuple(
            await self.session.scalars(
                select(TaskModel).where(
                    TaskModel.organization_id == self.org,
                    TaskModel.project_id == snapshot.project_id,
                    TaskModel.assignee_membership_id == member_id,
                )
            )
        )
        allowed = {t.id: t.version for t in rows}
        tasks = tuple(
            t
            for t in snapshot.tasks
            if t.id in allowed and t.assignee_id == member_id and t.task_version == allowed[t.id]
        )
        ids = {t.id for t in tasks}
        own_evidence = set(
            await self.session.scalars(
                select(EvidenceOriginalModel.id).where(
                    EvidenceOriginalModel.organization_id == self.org,
                    EvidenceOriginalModel.uploader_membership_id == member_id,
                )
            )
        )
        sources = tuple(
            s
            for s in snapshot.sources
            if s.task_id in ids
            and (s.kind == "BLOCKER" or (s.kind == "EVIDENCE" and s.evidence_id in own_evidence))
        )
        expected = (member_id,) if member_id in snapshot.window.expected_reporters else ()
        reported = (member_id,) if member_id in snapshot.window.reported_members else ()
        window = snapshot.window.model_copy(
            update={
                "expected_reporters": expected,
                "reported_members": reported,
                "full_coverage": bool(expected and reported),
                "coverage_state": "COMPLETE"
                if expected and reported
                else "PARTIAL"
                if expected
                else "NO_REPORTERS",
            }
        )
        return snapshot.model_copy(
            update={
                "scope": "OWN_WORK",
                "reporters": tuple(r for r in snapshot.reporters if r.membership_id == member_id),
                "window": window,
                "tasks": tasks,
                "sources": sources,
                "expected_count": len(expected),
                "reported_count": len(reported),
                "missing_reporters": tuple(m for m in expected if m not in reported),
                "unknown_inputs": tuple(
                    v for v in snapshot.unknown_inputs if any(str(t.id) in v for t in tasks)
                ),
            }
        )

    async def feed(self) -> tuple[SummaryDelivery, ...]:
        member = await self.session.get(MembershipModel, self.actor.membership_id)
        assert member is not None
        rows = await self.session.scalars(
            select(SummaryDeliveryModel)
            .where(
                SummaryDeliveryModel.organization_id == self.org,
                SummaryDeliveryModel.recipient_id == self.actor.membership_id,
                SummaryDeliveryModel.state == "DELIVERED",
            )
            .order_by(SummaryDeliveryModel.created_at.desc(), SummaryDeliveryModel.id)
            .limit(50)
        )
        result: list[SummaryDelivery] = []
        for row in rows:
            assert row.payload is not None
            snapshot = DailySummarySnapshot.model_validate(row.payload)
            if member.id not in {
                r.membership_id for r in await self.recipients(snapshot.project_id)
            }:
                continue
            result.append(
                SummaryDelivery(
                    id=row.id, snapshot=await self.project(snapshot, member.id, member.role)
                )
            )
        return tuple(result)


class DigestTransactions:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self.sessions = sessions

    @asynccontextmanager
    async def __call__(self, actor: AuthenticatedActor) -> AsyncGenerator[DigestRepository]:
        async with self.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true),"
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            yield DigestRepository(session, actor)
