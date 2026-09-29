"""PostgreSQL locks, RLS and append-only facts for manual reporting."""

import hashlib
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.audit.domain.events import AuditOutcome
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.planning_runs.adapters.database_models import OutboxEventModel
from app.modules.progress.adapters.daily_update_models import (
    DailyUpdateDraftModel,
    DailyUpdateDraftRevisionModel,
    DailyUpdateEvidenceLinkModel,
    DailyUpdateModel,
    TaskActualProjectionModel,
    TaskProgressObservationModel,
    WeeklyActualSnapshotModel,
    WorkLogModel,
)
from app.modules.progress.adapters.evidence_models import EvidenceOriginalModel
from app.modules.progress.application.daily_update_service import DailyUpdateRepository
from app.modules.progress.domain.daily_updates import (
    ConfirmedDailyUpdate,
    ConfirmedObservation,
    DailyUpdateDraft,
    DailyUpdateError,
    DailyUpdateItemInput,
    SelectedEvidence,
    TaskReportingContext,
)
from app.modules.work.adapters.database_models import (
    IdempotencyRecordModel,
    IdempotencyState,
    ProjectModel,
    TaskModel,
)
from app.modules.work.planning.adapters.database_models import ProjectWeekModel


class SqlAlchemyDailyUpdateRepository:
    def __init__(self, session: AsyncSession, actor: AuthenticatedActor):
        self.session, self.actor = session, actor
        self.org = actor.organization_id

    async def authenticate(self) -> None:
        active = await self.session.scalar(
            text(
                "SELECT public.lock_active_membership(:org,:member) AND EXISTS "
                "(SELECT 1 FROM memberships WHERE organization_id=:org AND "
                "id=:member AND user_id=:user)"
            ),
            {"org": self.org, "member": self.actor.membership_id, "user": self.actor.user_id},
        )
        if active is not True:
            raise DailyUpdateError("FORBIDDEN", 403)

    async def lock(self, scope: str) -> None:
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"{self.org}:daily-update:{scope}"},
        )

    async def replay(self, operation: str, key: str, fingerprint: str) -> dict[str, object] | None:
        await self.lock(f"idempotency:{self.actor.membership_id}:{operation}:{key}")
        row = await self.session.scalar(
            select(IdempotencyRecordModel).where(
                IdempotencyRecordModel.organization_id == self.org,
                IdempotencyRecordModel.actor_membership_id == self.actor.membership_id,
                IdempotencyRecordModel.operation == operation,
                IdempotencyRecordModel.idempotency_key == key,
            )
        )
        if row is None:
            return None
        if row.request_fingerprint != fingerprint:
            raise DailyUpdateError("IDEMPOTENCY_CONFLICT")
        return row.response_body

    async def remember(
        self, operation: str, key: str, fingerprint: str, result: dict[str, object]
    ) -> None:
        self.session.add(
            IdempotencyRecordModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id,
                operation=operation,
                idempotency_key=key,
                request_fingerprint=fingerprint,
                state=IdempotencyState.COMPLETED,
                response_status=201,
                response_body=result,
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )

    async def _task(self, task_id: UUID, own: bool = True) -> TaskModel:
        task = await self.session.scalar(
            select(TaskModel).where(TaskModel.organization_id == self.org, TaskModel.id == task_id)
        )
        if task is None or (own and task.assignee_membership_id != self.actor.membership_id):
            raise DailyUpdateError("RESOURCE_NOT_FOUND", 404)
        return task

    async def draft(self, draft_id: UUID) -> DailyUpdateDraft:
        row = await self.session.scalar(
            select(DailyUpdateDraftModel)
            .where(
                DailyUpdateDraftModel.organization_id == self.org,
                DailyUpdateDraftModel.id == draft_id,
                DailyUpdateDraftModel.owner_membership_id == self.actor.membership_id,
            )
            .with_for_update()
        )
        if row is None:
            raise DailyUpdateError("RESOURCE_NOT_FOUND", 404)
        revision = await self.session.scalar(
            select(DailyUpdateDraftRevisionModel).where(
                DailyUpdateDraftRevisionModel.organization_id == self.org,
                DailyUpdateDraftRevisionModel.draft_id == row.id,
                DailyUpdateDraftRevisionModel.version == row.version,
            )
        )
        assert revision is not None
        return DailyUpdateDraft.model_validate(
            {**revision.payload, "confirmed_update_id": row.confirmed_update_id}
        )

    async def save_draft(
        self,
        items: tuple[DailyUpdateItemInput, ...],
        timezone: str,
        draft_id: UUID | None,
        expected_version: int | None,
    ) -> DailyUpdateDraft:
        for item in items:
            task = await self._task(item.task_id)
            if task.version != item.expected_task_version:
                raise DailyUpdateError("STALE_TASK")
            await self._evidence(item, datetime.now(UTC), lock=False)
        if draft_id:
            previous = await self.draft(draft_id)
            if previous.version != expected_version or previous.confirmed_update_id:
                raise DailyUpdateError("STALE_DRAFT")
            version = previous.version + 1
            row = await self.session.get(DailyUpdateDraftModel, draft_id)
            assert row is not None
            row.version = version
        else:
            draft_id, version = uuid4(), 1
            self.session.add(
                DailyUpdateDraftModel(
                    id=draft_id,
                    organization_id=self.org,
                    owner_membership_id=self.actor.membership_id,
                    version=version,
                )
            )
            await self.session.flush()
        content_hash = hashlib.sha256(
            json.dumps([i.model_dump(mode="json") for i in items], sort_keys=True).encode()
        ).hexdigest()
        result = DailyUpdateDraft(
            id=draft_id,
            version=version,
            content_hash=content_hash,
            items=items,
            reporting_timezone=timezone,
        )
        self.session.add(
            DailyUpdateDraftRevisionModel(
                id=uuid4(),
                organization_id=self.org,
                draft_id=draft_id,
                version=version,
                owner_membership_id=self.actor.membership_id,
                content_hash=content_hash,
                payload=result.model_dump(mode="json"),
            )
        )
        return result

    async def _evidence(self, item: DailyUpdateItemInput, at: datetime, lock: bool) -> None:
        for ref in sorted(item.evidence_refs, key=lambda r: (r.evidence_id, r.version)):
            query = select(EvidenceOriginalModel).where(
                EvidenceOriginalModel.organization_id == self.org,
                EvidenceOriginalModel.id == ref.evidence_id,
                EvidenceOriginalModel.version == ref.version,
                EvidenceOriginalModel.uploader_membership_id == self.actor.membership_id,
            )
            row = await self.session.scalar(query.with_for_update() if lock else query)
            if (
                row is None
                or row.state != "READY"
                or (row.confirmed_at is None and row.expires_at <= at)
            ):
                raise DailyUpdateError("INVALID_EVIDENCE", 422)
            # Staged originals may be explicitly linked to this Task. Reused originals must
            # already belong to this Task; selecting proof from another Task is not presence.
            if row.confirmed_at is not None:
                related = await self.session.scalar(
                    select(DailyUpdateEvidenceLinkModel.id)
                    .join(
                        TaskProgressObservationModel,
                        (
                            TaskProgressObservationModel.organization_id
                            == DailyUpdateEvidenceLinkModel.organization_id
                        )
                        & (
                            TaskProgressObservationModel.id
                            == DailyUpdateEvidenceLinkModel.observation_id
                        ),
                    )
                    .where(
                        DailyUpdateEvidenceLinkModel.organization_id == self.org,
                        DailyUpdateEvidenceLinkModel.evidence_id == ref.evidence_id,
                        TaskProgressObservationModel.task_id == item.task_id,
                    )
                    .limit(1)
                )
                if related is None:
                    raise DailyUpdateError("UNRELATED_EVIDENCE", 422)

    async def _latest_actual(self, task_id: UUID) -> tuple[UUID, Decimal] | None:
        result = (
            await self.session.execute(
                text(
                    "SELECT observation_id, reported_percent "
                    "FROM public.read_task_reported_actual(:task)"
                ),
                {"task": task_id},
            )
        ).one_or_none()
        return (result[0], result[1]) if result else None

    async def confirm(self, draft: DailyUpdateDraft, at: datetime) -> ConfirmedDailyUpdate:
        # Match the planning lock order: project, then sorted Tasks, then work-log dates.
        task_ids = sorted(i.task_id for i in draft.items)
        preliminary = [await self._task(task_id) for task_id in task_ids]
        for project_id in sorted({t.project_id for t in preliminary}):
            await self.session.scalar(
                select(ProjectModel.id)
                .where(ProjectModel.organization_id == self.org, ProjectModel.id == project_id)
                .with_for_update()
            )
        tasks = (
            await self.session.scalars(
                select(TaskModel)
                .where(TaskModel.organization_id == self.org, TaskModel.id.in_(task_ids))
                .order_by(TaskModel.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
        by_id = {t.id: t for t in tasks}
        projections: dict[UUID, TaskActualProjectionModel] = {}
        corrections: dict[UUID, TaskProgressObservationModel] = {}
        dates = {i.reporting_date for i in draft.items}
        for item in draft.items:
            task = by_id.get(item.task_id)
            if task is None or task.assignee_membership_id != self.actor.membership_id:
                raise DailyUpdateError("RESOURCE_NOT_FOUND", 404)
            if task.version != item.expected_task_version:
                raise DailyUpdateError("STALE_TASK")
            projection = await self.session.scalar(
                select(TaskActualProjectionModel)
                .where(
                    TaskActualProjectionModel.organization_id == self.org,
                    TaskActualProjectionModel.task_id == task.id,
                )
                .with_for_update()
            )
            if projection is None:
                projection = TaskActualProjectionModel(
                    id=uuid4(), organization_id=self.org, task_id=task.id, progress_version=0
                )
                self.session.add(projection)
                await self.session.flush()
            if projection.progress_version != item.expected_progress_version:
                raise DailyUpdateError("STALE_PROGRESS")
            projections[task.id] = projection
            if item.corrects_observation_id:
                old = await self.session.scalar(
                    select(TaskProgressObservationModel).where(
                        TaskProgressObservationModel.organization_id == self.org,
                        TaskProgressObservationModel.id == item.corrects_observation_id,
                        TaskProgressObservationModel.task_id == task.id,
                        TaskProgressObservationModel.owner_membership_id
                        == self.actor.membership_id,
                    )
                )
                if old is None:
                    raise DailyUpdateError("INVALID_CORRECTION", 422)
                superseded = await self.session.scalar(
                    select(TaskProgressObservationModel.id).where(
                        TaskProgressObservationModel.organization_id == self.org,
                        TaskProgressObservationModel.corrects_observation_id == old.id,
                    )
                )
                if superseded:
                    raise DailyUpdateError("STALE_CORRECTION")
                corrections[task.id] = old
                dates.add(old.reporting_date)
            latest = await self._latest_actual(task.id)
            if latest and item.reported_percent < latest[1] and not item.correction_reason.strip():
                raise DailyUpdateError("CORRECTION_REASON_REQUIRED", 422)
        for day in sorted(dates):
            await self.lock(f"hours:{self.actor.membership_id}:{day}")
        for day in sorted(dates):
            total = await self.session.scalar(
                text(
                    "SELECT coalesce(sum(w.spent_hours),0) FROM work_logs w WHERE "
                    "w.organization_id=:org AND w.owner_membership_id=:owner AND "
                    "w.reporting_date=:day AND NOT EXISTS (SELECT 1 FROM "
                    "task_progress_observations o WHERE "
                    "o.organization_id=w.organization_id AND "
                    "o.corrects_observation_id=w.observation_id)"
                ),
                {"org": self.org, "owner": self.actor.membership_id, "day": day},
            )
            hours = Decimal(str(total))
            for item in draft.items:
                old = corrections.get(item.task_id)
                if old and old.reporting_date == day:
                    old_log = await self.session.scalar(
                        select(WorkLogModel).where(
                            WorkLogModel.organization_id == self.org,
                            WorkLogModel.observation_id == old.id,
                        )
                    )
                    if old_log:
                        hours -= old_log.spent_hours
                if item.reporting_date == day:
                    hours += item.spent_hours or Decimal(0)
            if hours > 24:
                raise DailyUpdateError("DAILY_HOURS_LIMIT", 422)
        # Evidence locks are acquired globally in ID order to avoid multi-item deadlocks.
        for evidence_id in sorted(
            {ref.evidence_id for item in draft.items for ref in item.evidence_refs}
        ):
            await self.lock(f"evidence:{evidence_id}")
        for item in sorted(
            draft.items,
            key=lambda i: str(min((r.evidence_id for r in i.evidence_refs), default=UUID(int=0))),
        ):
            await self._evidence(item, at, lock=True)
        update_id = uuid4()
        self.session.add(
            DailyUpdateModel(
                id=update_id,
                organization_id=self.org,
                draft_id=draft.id,
                draft_version=draft.version,
                owner_membership_id=self.actor.membership_id,
                confirmed_at=at,
            )
        )
        await self.session.flush()
        results: list[ConfirmedObservation] = []
        for item in draft.items:
            task = by_id[item.task_id]
            projection = projections[task.id]
            projection.progress_version += 1
            week = (
                await self.session.get(ProjectWeekModel, task.project_week_id)
                if task.project_week_id
                else None
            )
            reporting_at = datetime.combine(
                item.reporting_date, time.min, ZoneInfo(draft.reporting_timezone)
            ).astimezone(UTC)
            result = ConfirmedObservation(
                id=uuid4(),
                update_id=update_id,
                item=item,
                progress_version=projection.progress_version,
                reporting_at=reporting_at,
                confirmed_at=at,
                reporting_timezone=draft.reporting_timezone,
                late=bool(week and week.status == "COMPLETED"),
                project_week_state="LINKED" if week else "NO_PROJECT_WEEK",
            )
            self.session.add(
                TaskProgressObservationModel(
                    id=result.id,
                    organization_id=self.org,
                    update_id=update_id,
                    task_id=task.id,
                    owner_membership_id=self.actor.membership_id,
                    progress_version=projection.progress_version,
                    reporting_date=item.reporting_date,
                    reporting_at=reporting_at,
                    confirmed_at=at,
                    reported_percent=item.reported_percent,
                    remaining_hours=item.remaining_hours,
                    corrects_observation_id=item.corrects_observation_id,
                    payload=result.model_dump(mode="json"),
                )
            )
            await self.session.flush()
            if item.spent_hours is not None:
                old_log = (
                    await self.session.scalar(
                        select(WorkLogModel).where(
                            WorkLogModel.organization_id == self.org,
                            WorkLogModel.observation_id == item.corrects_observation_id,
                        )
                    )
                    if item.corrects_observation_id
                    else None
                )
                self.session.add(
                    WorkLogModel(
                        id=uuid4(),
                        organization_id=self.org,
                        observation_id=result.id,
                        owner_membership_id=self.actor.membership_id,
                        reporting_date=item.reporting_date,
                        spent_hours=item.spent_hours,
                        supersedes_log_id=old_log.id if old_log else None,
                    )
                )
            for ref in item.evidence_refs:
                original = await self.session.get(EvidenceOriginalModel, ref.evidence_id)
                assert original is not None
                original.confirmed_at = original.confirmed_at or at
                self.session.add(
                    DailyUpdateEvidenceLinkModel(
                        id=uuid4(),
                        organization_id=self.org,
                        observation_id=result.id,
                        evidence_id=ref.evidence_id,
                        evidence_version=ref.version,
                    )
                )
            # Only effective observations participate; historical/backdated reports remain visible.
            latest = await self._latest_actual(task.id)
            projection.observation_id = latest[0] if latest else None
            results.append(result)
        row = await self.session.get(DailyUpdateDraftModel, draft.id)
        assert row is not None
        row.confirmed_update_id = update_id
        week_ids = {task.project_week_id for task in by_id.values()} - {None}
        for week_id in sorted(value for value in week_ids if value is not None):
            week_results = [r for r in results if by_id[r.item.task_id].project_week_id == week_id]
            self.session.add(
                WeeklyActualSnapshotModel(
                    id=uuid4(),
                    organization_id=self.org,
                    project_week_id=week_id,
                    owner_membership_id=self.actor.membership_id,
                    update_id=update_id,
                    kind="LATE" if week_results[0].late else "CURRENT",
                    captured_at=at,
                    payload={"observations": [r.model_dump(mode="json") for r in week_results]},
                )
            )
        event_id = uuid4()
        self.session.add(
            OutboxEventModel(
                id=event_id,
                organization_id=self.org,
                event_id=event_id,
                event_type="daily_update.confirmed",
                aggregate_type="daily_update",
                aggregate_id=update_id,
                envelope_version="1.0",
                payload={
                    "update_id": str(update_id),
                    "task_ids": [str(i.task_id) for i in draft.items],
                },
            )
        )
        await self.session.flush()
        return ConfirmedDailyUpdate(id=update_id, draft_id=draft.id, observations=tuple(results))

    async def history(self, task_id: UUID) -> tuple[ConfirmedObservation, ...]:
        await self._task(task_id, own=False)
        rows = (
            await self.session.scalars(
                select(TaskProgressObservationModel)
                .where(
                    TaskProgressObservationModel.organization_id == self.org,
                    TaskProgressObservationModel.task_id == task_id,
                )
                .order_by(TaskProgressObservationModel.progress_version.desc())
            )
        ).all()
        return tuple(ConfirmedObservation.model_validate(r.payload) for r in rows)

    async def context(self, task_id: UUID, timezone: str, at: datetime) -> TaskReportingContext:
        task = await self._task(task_id)
        projection = await self.session.scalar(
            select(TaskActualProjectionModel).where(
                TaskActualProjectionModel.organization_id == self.org,
                TaskActualProjectionModel.task_id == task_id,
            )
        )
        observation = (
            await self.session.get(TaskProgressObservationModel, projection.observation_id)
            if projection and projection.observation_id
            else None
        )
        links = (
            await self.session.scalars(
                select(DailyUpdateEvidenceLinkModel)
                .join(
                    TaskProgressObservationModel,
                    (
                        TaskProgressObservationModel.organization_id
                        == DailyUpdateEvidenceLinkModel.organization_id
                    )
                    & (
                        TaskProgressObservationModel.id
                        == DailyUpdateEvidenceLinkModel.observation_id
                    ),
                )
                .where(
                    DailyUpdateEvidenceLinkModel.organization_id == self.org,
                    TaskProgressObservationModel.task_id == task_id,
                )
            )
        ).all()
        refs = tuple(
            sorted(
                {
                    SelectedEvidence(evidence_id=link.evidence_id, version=link.evidence_version)
                    for link in links
                },
                key=lambda r: (r.evidence_id, r.version),
            )
        )
        return TaskReportingContext(
            task_id=task_id,
            task_version=task.version,
            progress_version=projection.progress_version if projection else 0,
            reported_percent=observation.reported_percent if observation else None,
            remaining_hours=observation.remaining_hours if observation else None,
            reporting_timezone=timezone,
            reporting_date=at.astimezone(ZoneInfo(timezone)).date(),
            project_week_state="LINKED" if task.project_week_id else "NO_PROJECT_WEEK",
            evidence_refs=refs,
        )

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
                resource_type="daily_update",
                resource_id=resource_id,
                request_id=request_id,
                idempotency_key=key,
                before_data={},
                after_data={},
                reason_data={"code": code} if code else {},
            )
        )


class SqlAlchemyDailyUpdateTransactions:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self.sessions = sessions

    @asynccontextmanager
    async def __call__(self, actor: AuthenticatedActor) -> AsyncGenerator[DailyUpdateRepository]:
        async with self.sessions.begin() as session:
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true), "
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            yield SqlAlchemyDailyUpdateRepository(session, actor)
