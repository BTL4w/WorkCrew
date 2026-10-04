"""Reports, source inventories, audit and replay share one authorized SQL transaction."""

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.audit.domain.events import AuditOutcome
from app.modules.automations.adapters.database_models import ScheduleModel, ScheduleVersionModel
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.planning_runs.adapters.database_models import OutboxEventModel
from app.modules.work.adapters.database_models import (
    IdempotencyRecordModel,
    IdempotencyState,
    ProjectModel,
)

from ..application.ports import ReportSnapshotReadPort
from ..domain.commands import CreateReportCommand
from ..domain.events import MetricsCaptured
from ..domain.reports import Report, ReportError, ReportPage, ReportResult, ReportVersion
from ..domain.snapshots import ReportMetricSnapshot
from .database_models import (
    ReportModel,
    ReportReceiptModel,
    ReportSnapshotModel,
    ReportSourceModel,
    ReportVersionModel,
)
from .snapshot_reader import SQLReportSnapshotReader


def report_domain(row: ReportModel) -> Report:
    return Report.model_validate({key: getattr(row, key) for key in Report.model_fields})


def version_domain(row: ReportVersionModel) -> ReportVersion:
    return ReportVersion.model_validate(
        {key: getattr(row, key) for key in ReportVersion.model_fields}
    )


class SQLReportRepository:
    def __init__(self, session: AsyncSession, actor: AuthenticatedActor, default_timezone: str):
        self.session, self.actor, self.org = session, actor, actor.organization_id
        self.default_timezone = default_timezone
        self.snapshot_reader: ReportSnapshotReadPort = SQLReportSnapshotReader(session, self.org)

    async def authenticate(self) -> None:
        active = await self.session.scalar(
            text(
                "SELECT public.lock_active_membership(:org,:member) "
                "AND EXISTS(SELECT 1 FROM memberships WHERE organization_id=:org AND id=:member "
                "AND user_id=:user AND role IN ('MANAGER','ADMIN'))"
            ),
            {"org": self.org, "member": self.actor.membership_id, "user": self.actor.user_id},
        )
        if active is not True:
            raise ReportError("FORBIDDEN", 403)

    async def authorize_project(self, project_id: UUID) -> None:
        found = await self.session.scalar(
            select(ProjectModel.id).where(
                ProjectModel.organization_id == self.org, ProjectModel.id == project_id
            )
        )
        if found is None:
            raise ReportError("RESOURCE_NOT_FOUND", 404)

    async def captured_at(self) -> datetime:
        result = await self.session.scalar(select(func.transaction_timestamp()))
        if not isinstance(result, datetime):
            raise ReportError("REPORT_CAPTURE_FAILED", 409)
        return result

    async def timezone(self, project_id: UUID) -> str:
        row = await self.session.scalar(
            select(ScheduleModel).where(
                ScheduleModel.organization_id == self.org, ScheduleModel.project_id == project_id
            )
        )
        if row is not None:
            config = await self.session.scalar(
                select(ScheduleVersionModel).where(
                    ScheduleVersionModel.organization_id == self.org,
                    ScheduleVersionModel.schedule_id == row.id,
                    ScheduleVersionModel.version == row.version,
                )
            )
            if config is not None:
                value = config.payload.get("timezone")
                if isinstance(value, str):
                    return value
        return self.default_timezone

    async def replay(self, key: str, fingerprint: str) -> UUID | None:
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"{self.org}:report.create:{self.actor.membership_id}:{key}"},
        )
        row = await self.session.scalar(
            select(IdempotencyRecordModel).where(
                IdempotencyRecordModel.organization_id == self.org,
                IdempotencyRecordModel.actor_membership_id == self.actor.membership_id,
                IdempotencyRecordModel.operation == "report.create",
                IdempotencyRecordModel.idempotency_key == key,
            )
        )
        if row is None:
            return None
        if row.request_fingerprint != fingerprint or row.response_body is None:
            raise ReportError("IDEMPOTENCY_KEY_REUSED", 409)
        return UUID(str(row.response_body["report_id"]))

    async def save(
        self,
        snapshot: ReportMetricSnapshot,
        command: CreateReportCommand,
        version_id: UUID,
        key: str,
        fingerprint: str,
        request_id: str,
    ) -> ReportResult:
        report = Report(
            id=snapshot.report_id,
            organization_id=self.org,
            project_id=command.project_id,
            kind=command.kind,
            locale=command.locale,
            version=1,
            snapshot_id=snapshot.id,
            selected_version_id=version_id,
            created_by_membership_id=self.actor.membership_id,
            narrative_requested=command.narrative_enabled,
            created_at=snapshot.captured_at,
        )
        version = ReportVersion(
            id=version_id,
            report_id=report.id,
            snapshot_id=snapshot.id,
            locale=command.locale,
            created_at=snapshot.captured_at,
        )
        self.session.add(ReportModel(**report.model_dump(mode="python")))
        self.session.add(
            ReportSnapshotModel(
                id=snapshot.id,
                organization_id=self.org,
                report_id=report.id,
                project_id=command.project_id,
                snapshot_hash=snapshot.snapshot_hash,
                payload=snapshot.model_dump(mode="json"),
                captured_at=snapshot.captured_at,
            )
        )
        self.session.add(
            ReportVersionModel(organization_id=self.org, **version.model_dump(mode="python"))
        )
        # Parent facts must be flushed before indexed sources/receipts (nondeferrable FKs).
        await self.session.flush()
        for source in snapshot.sources:
            self.session.add(
                ReportSourceModel(
                    id=uuid4(),
                    organization_id=self.org,
                    snapshot_id=snapshot.id,
                    task_id=source.resource_id,
                    payload=source.model_dump(mode="json"),
                )
            )
        for receipt in snapshot.receipts:
            self.session.add(
                ReportReceiptModel(
                    id=receipt.id,
                    organization_id=self.org,
                    snapshot_id=snapshot.id,
                    payload=receipt.model_dump(mode="json"),
                )
            )
        self.session.add(
            IdempotencyRecordModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id,
                operation="report.create",
                idempotency_key=key,
                request_fingerprint=fingerprint,
                state=IdempotencyState.COMPLETED,
                response_status=201,
                response_body={"report_id": str(report.id)},
                expires_at=snapshot.captured_at + timedelta(days=7),
            )
        )
        self.session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id,
                action="report.created",
                outcome=AuditOutcome.SUCCEEDED,
                resource_type="report",
                resource_id=report.id,
                request_id=request_id,
                idempotency_key=key,
                before_data={},
                after_data={
                    "snapshot_id": str(snapshot.id),
                    "snapshot_hash": snapshot.snapshot_hash,
                },
                reason_data={},
            )
        )
        self.session.add(
            OutboxEventModel(
                id=uuid4(),
                organization_id=self.org,
                event_id=uuid4(),
                event_type="report.metrics_captured.v1",
                aggregate_type="report",
                aggregate_id=report.id,
                payload=MetricsCaptured(
                    report_id=report.id,
                    snapshot_id=snapshot.id,
                    snapshot_hash=snapshot.snapshot_hash,
                    actor_membership_id=self.actor.membership_id,
                ).model_dump(mode="json"),
                status="PENDING",
            )
        )
        await self.session.flush()
        return ReportResult(
            report=report,
            snapshot=snapshot,
            selected_version=version,
            generation_state="AI_UNAVAILABLE" if command.narrative_enabled else "NOT_REQUESTED",
        )

    async def get(self, report_id: UUID, *, replayed: bool = False) -> ReportResult:
        row = await self.session.scalar(
            select(ReportModel).where(
                ReportModel.organization_id == self.org, ReportModel.id == report_id
            )
        )
        if row is None:
            raise ReportError("RESOURCE_NOT_FOUND", 404)
        await self.authorize_project(row.project_id)
        snapshot_row = await self.session.scalar(
            select(ReportSnapshotModel).where(
                ReportSnapshotModel.organization_id == self.org,
                ReportSnapshotModel.id == row.snapshot_id,
                ReportSnapshotModel.report_id == row.id,
            )
        )
        version_row = await self.session.scalar(
            select(ReportVersionModel).where(
                ReportVersionModel.organization_id == self.org,
                ReportVersionModel.id == row.selected_version_id,
                ReportVersionModel.report_id == row.id,
            )
        )
        if snapshot_row is None or version_row is None:
            raise ReportError("REPORT_CAPTURE_FAILED", 409)
        snapshot = ReportMetricSnapshot.model_validate(snapshot_row.payload)
        if not snapshot.verified_hash() or snapshot.snapshot_hash != snapshot_row.snapshot_hash:
            raise ReportError("REPORT_CAPTURE_FAILED", 409)
        return ReportResult(
            report=report_domain(row),
            snapshot=snapshot,
            selected_version=version_domain(version_row),
            generation_state="AI_UNAVAILABLE" if row.narrative_requested else "NOT_REQUESTED",
            replayed=replayed,
        )

    async def list(self, project_id: UUID, page: int, page_size: int) -> ReportPage:
        await self.authorize_project(project_id)
        predicate = (ReportModel.organization_id == self.org, ReportModel.project_id == project_id)
        count = await self.session.scalar(
            select(func.count()).select_from(ReportModel).where(*predicate)
        )
        rows = await self.session.scalars(
            select(ReportModel)
            .where(*predicate)
            .order_by(ReportModel.created_at.desc(), ReportModel.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return ReportPage(
            items=tuple(report_domain(row) for row in rows),
            page=page,
            page_size=page_size,
            total=count or 0,
        )

    async def audit_rejection(
        self, request_id: str, key: str | None, code: str, project_id: UUID | None
    ) -> None:
        self.session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id,
                action="report.created",
                outcome=AuditOutcome.REJECTED,
                resource_type="project",
                resource_id=project_id,
                request_id=request_id,
                idempotency_key=key,
                before_data={},
                after_data={},
                reason_data={"reason_code": code},
            )
        )
        await self.session.flush()
