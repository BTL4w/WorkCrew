"""Capture all authorized Task rows once within the owning REPEATABLE READ transaction."""

from collections import Counter
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.work.adapters.database_models import TaskModel

from ..domain.commands import CaptureReportCommand
from ..domain.metrics import AggregateReceipt, MetricState, MetricValue, SourceRef
from ..domain.snapshots import ReportMetricSnapshot, canonical_hash


class SQLReportSnapshotReader:
    def __init__(self, session: AsyncSession, organization_id: UUID):
        self.session, self.org = session, organization_id

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
        counts = Counter(row.status.value for row in rows)
        source_index = tuple(
            SourceRef(
                resource_type="TASK",
                resource_id=row.id,
                version=row.version,
                observed_at=command.captured_at,
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
        metrics: dict[str, MetricValue] = {}
        for key, count in [
            ("total", len(rows)),
            ("to_do", counts["TO_DO"]),
            ("in_progress", counts["IN_PROGRESS"]),
            ("done", counts["DONE"]),
        ]:
            name = f"tasks.status.{key}_count"
            metrics[name] = MetricValue(
                key=name,
                value=Decimal(count),
                unit="COUNT",
                state=MetricState.KNOWN,
                time_basis="AT_CAPTURE",
            )
        key = "tasks.deadline.unknown_count"
        metrics[key] = MetricValue(
            key=key,
            value=Decimal(sum(row.due_date is None for row in rows)),
            unit="COUNT",
            state=MetricState.KNOWN,
            time_basis="AT_CAPTURE",
        )
        key = "progress.observation_coverage"
        metrics[key] = MetricValue(
            key=key,
            value=None,
            unit="FRACTION",
            state=MetricState.UNKNOWN if rows else MetricState.NOT_APPLICABLE,
            time_basis="AT_CAPTURE",
            limitations=("PROGRESS_COVERAGE_NOT_CAPTURED",) if rows else ("NO_TASKS",),
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
