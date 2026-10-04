"""Capture all authorized Task rows once within the owning REPEATABLE READ transaction."""

from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.progress.adapters.daily_update_models import (
    DailyUpdateDraftRevisionModel,
    DailyUpdateModel,
    TaskActualProjectionModel,
    TaskProgressObservationModel,
    WorkLogModel,
)
from app.modules.work.adapters.database_models import TaskModel

from ..domain.catalog import ObservationInput, TaskInput, WorkLogInput, core_metrics
from ..domain.commands import CaptureReportCommand
from ..domain.metrics import AggregateReceipt, SourceRef
from ..domain.snapshots import ReportMetricSnapshot, canonical_hash
from .auxiliary_reader import AuxiliaryReader


class SQLReportSnapshotReader:
    def __init__(self, session: AsyncSession, actor: AuthenticatedActor, timezone: str):
        self.session, self.org, self.actor, self.timezone = (
            session,
            actor.organization_id,
            actor,
            timezone,
        )

    async def capture(self, command: CaptureReportCommand) -> ReportMetricSnapshot:
        isolation = await self.session.scalar(text("SHOW transaction_isolation"))
        if isolation != "repeatable read":
            raise ValueError("consistent reporting transaction required")
        rows = tuple(
            await self.session.scalars(
                select(TaskModel)
                .where(
                    TaskModel.organization_id == self.org,
                    TaskModel.project_id == command.project_id,
                )
                .order_by(TaskModel.id)
            )
        )
        source_index = tuple(
            SourceRef(
                resource_type="TASK",
                resource_id=row.id,
                version=row.version,
                observed_at=command.captured_at,
                label=row.title,
                facts={
                    "status": row.status.value,
                    "due_date": row.due_date.isoformat() if row.due_date else None,
                    "estimated_effort_hours": row.estimated_effort_hours,
                },
                fingerprint=canonical_hash(
                    {
                        "id": str(row.id),
                        "version": row.version,
                        "status": row.status.value,
                        "due_date": row.due_date.isoformat() if row.due_date else None,
                    }
                ),
            )
            for row in rows
        )
        task_by_id = {row.id: row for row in rows}
        observed = tuple(
            await self.session.scalars(
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
                    TaskProgressObservationModel.organization_id == self.org,
                    TaskProgressObservationModel.task_id.in_(task_by_id),
                    TaskProgressObservationModel.confirmed_at <= command.captured_at,
                )
            )
        )
        observations = tuple(
            ObservationInput(
                o.id,
                o.task_id,
                o.progress_version,
                o.reported_percent,
                o.remaining_hours,
                o.confirmed_at,
                str(o.payload.get("reporting_timezone", "UTC")),
                o.owner_membership_id != task_by_id[o.task_id].assignee_membership_id,
            )
            for o in observed
        )
        logs = tuple(
            (
                await self.session.execute(
                    select(
                        WorkLogModel, TaskProgressObservationModel, DailyUpdateDraftRevisionModel
                    )
                    .join(
                        TaskProgressObservationModel,
                        (
                            TaskProgressObservationModel.organization_id
                            == WorkLogModel.organization_id
                        )
                        & (TaskProgressObservationModel.id == WorkLogModel.observation_id),
                    )
                    .join(
                        DailyUpdateModel,
                        (
                            DailyUpdateModel.organization_id
                            == TaskProgressObservationModel.organization_id
                        )
                        & (DailyUpdateModel.id == TaskProgressObservationModel.update_id),
                    )
                    .join(
                        DailyUpdateDraftRevisionModel,
                        (
                            DailyUpdateDraftRevisionModel.organization_id
                            == DailyUpdateModel.organization_id
                        )
                        & (DailyUpdateDraftRevisionModel.draft_id == DailyUpdateModel.draft_id)
                        & (DailyUpdateDraftRevisionModel.version == DailyUpdateModel.draft_version),
                    )
                    .where(
                        WorkLogModel.organization_id == self.org,
                        TaskProgressObservationModel.task_id.in_(task_by_id),
                        TaskProgressObservationModel.confirmed_at <= command.captured_at,
                    )
                )
            ).all()
        )
        work_logs = tuple(
            WorkLogInput(
                log.id,
                log.observation_id,
                o.task_id,
                log.reporting_date,
                log.spent_hours,
                log.supersedes_log_id,
                str(revision.payload.get("reporting_timezone", "UTC")),
            )
            for log, o, revision in logs
        )
        metrics = core_metrics(
            tuple(
                TaskInput(
                    row.id,
                    row.status.value,
                    row.created_at,
                    row.due_date,
                    Decimal(row.estimated_effort_hours)
                    if row.estimated_effort_hours is not None
                    else None,
                )
                for row in rows
            ),
            observations,
            work_logs,
            command.period,
            command.captured_at,
        )
        auxiliary = await AuxiliaryReader(self.session, self.actor, self.timezone).capture(
            command, rows
        )
        metrics.update(auxiliary.metrics)
        source_index += auxiliary.sources
        source_index += tuple(
            SourceRef(
                resource_type="OBSERVATION",
                resource_id=o.id,
                version=o.progress_version,
                observed_at=command.captured_at,
                label=task_by_id[o.task_id].title,
                facts={
                    "task_id": str(o.task_id),
                    "reported_percent": str(o.reported_percent),
                    "reporting_timezone": str(o.payload.get("reporting_timezone", "UTC")),
                    "remaining_hours": str(o.remaining_hours)
                    if o.remaining_hours is not None
                    else None,
                    "reporting_date": o.reporting_date.isoformat(),
                    "confirmed_at": o.confirmed_at.isoformat(),
                },
                fingerprint=canonical_hash(o.payload),
            )
            for o in observed
        )
        source_index += tuple(
            SourceRef(
                resource_type="WORK_LOG",
                resource_id=log.id,
                version=1,
                observed_at=command.captured_at,
                label=task_by_id[o.task_id].title,
                facts={
                    "task_id": str(o.task_id),
                    "reporting_date": log.reporting_date.isoformat(),
                    "spent_hours": str(log.spent_hours),
                    "supersedes_log_id": str(log.supersedes_log_id)
                    if log.supersedes_log_id
                    else None,
                    "reporting_timezone": str(revision.payload.get("reporting_timezone", "UTC")),
                },
                fingerprint=canonical_hash(
                    {
                        "observation_id": str(log.observation_id),
                        "spent_hours": str(log.spent_hours),
                        "reporting_date": log.reporting_date.isoformat(),
                    }
                ),
            )
            for log, o, revision in logs
        )
        deduplicated: dict[tuple[str, UUID], SourceRef] = {}
        for source in source_index:
            deduplicated.setdefault((source.resource_type, source.resource_id), source)
        for key, value in metrics.items():
            metrics[key] = value.model_copy(
                update={
                    "source_refs": tuple(
                        deduplicated[(ref.resource_type, ref.resource_id)]
                        for ref in value.source_refs
                    )
                }
            )
        source_index = tuple(
            sorted(
                deduplicated.values(),
                key=lambda source: (source.resource_type, str(source.resource_id), source.version),
            )
        )
        receipt = AggregateReceipt(
            id=uuid4(),
            project_id=command.project_id,
            metric_keys=tuple(metrics),
            row_count=len(rows),
            captured_at=command.captured_at,
            scope_hash=canonical_hash(
                {
                    "organization_id": str(self.org),
                    "project_id": str(command.project_id),
                    "sources": [s.model_dump(mode="json") for s in source_index],
                }
            ),
        )
        snapshot = ReportMetricSnapshot(
            id=command.snapshot_id,
            organization_id=self.org,
            report_id=command.report_id,
            project_id=command.project_id,
            period=command.period,
            captured_at=command.captured_at,
            snapshot_hash="0" * 64,
            metrics=metrics,
            sources=source_index,
            receipts=(receipt,),
            limitations=("CURRENT_STATE_AT_CAPTURE",),
        )
        return snapshot.model_copy(
            update={
                "snapshot_hash": canonical_hash(
                    snapshot.model_dump(mode="json", exclude={"snapshot_hash"})
                )
            }
        )
