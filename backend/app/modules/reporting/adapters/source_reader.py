"""Current tenant-authorized source existence/version checks for captured references."""

from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.automations.adapters.database_models import ReportingWindowModel
from app.modules.automations.domain.digests import SummarySource
from app.modules.automations.domain.schedules import ReportingWindow
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.people_capacity.adapters.database_models import CapacityEntryModel, LeaveEntryModel
from app.modules.progress.adapters.blocker_models import BlockerModel
from app.modules.progress.adapters.daily_update_models import (
    TaskProgressObservationModel,
    WorkLogModel,
)
from app.modules.progress.adapters.progress_models import WeeklyPlanBaselineModel
from app.modules.risk.adapters.database_models import RiskAssessmentModel, RiskJobModel
from app.modules.work.adapters.database_models import TaskModel

from ..domain.metrics import SourceRef
from ..domain.snapshots import canonical_hash
from .window_reader import window_facts

Freshness = Literal["CURRENT", "UPDATED", "UNAVAILABLE"]


async def source_freshness(
    session: AsyncSession, actor: AuthenticatedActor, source: SourceRef
) -> Freshness:
    org = actor.organization_id
    version: int | None = None
    if source.resource_type in {"DAILY_SUMMARY", "SUMMARY_SOURCE"}:
        from app.modules.automations.adapters.delivery_models import SummarySnapshotModel
        from app.modules.automations.domain.digests import DailySummarySnapshot

        summary_id = (
            source.resource_id
            if source.resource_type == "DAILY_SUMMARY"
            else UUID(str(source.facts.get("summary_id")))
        )
        row = await session.scalar(
            select(SummarySnapshotModel).where(
                SummarySnapshotModel.organization_id == org, SummarySnapshotModel.id == summary_id
            )
        )
        if row is None:
            return "UNAVAILABLE"
        summary = DailySummarySnapshot.model_validate(row.payload)
        digest_hash = canonical_hash(summary.model_dump(mode="json"))
        if digest_hash != (
            source.fingerprint
            if source.resource_type == "DAILY_SUMMARY"
            else source.facts.get("summary_hash")
        ):
            return "UNAVAILABLE"
        if source.resource_type == "SUMMARY_SOURCE":
            captured = next(
                (
                    s
                    for s in summary.sources
                    if s.id == source.resource_id
                    and s.version == source.version
                    and canonical_hash(s.model_dump(mode="json")) == source.fingerprint
                ),
                None,
            )
            if captured is None:
                return "UNAVAILABLE"
            return (
                "CURRENT"
                if await summary_source_available(session, org, summary.project_id, captured)
                else "UNAVAILABLE"
            )
        # The summary receipt covers every included source, even beyond the model detail cap.
        ids = {t.id for t in summary.tasks}
        visible = set(
            await session.scalars(
                select(TaskModel.id).where(
                    TaskModel.organization_id == org,
                    TaskModel.project_id == summary.project_id,
                    TaskModel.id.in_(ids),
                )
            )
        )
        if ids != visible:
            return "UNAVAILABLE"
        for captured in summary.sources:
            if not await summary_source_available(session, org, summary.project_id, captured):
                return "UNAVAILABLE"
        return "CURRENT"
    if source.resource_type == "TASK":
        version = await session.scalar(
            select(TaskModel.version).where(
                TaskModel.organization_id == org, TaskModel.id == source.resource_id
            )
        )
    elif source.resource_type in ("CAPACITY", "LEAVE"):
        model = CapacityEntryModel if source.resource_type == "CAPACITY" else LeaveEntryModel
        version = await session.scalar(
            select(model.version).where(
                model.organization_id == org, model.id == source.resource_id
            )
        )
    elif source.resource_type == "BLOCKER":
        version = await session.scalar(
            select(BlockerModel.version).where(
                BlockerModel.organization_id == org, BlockerModel.id == source.resource_id
            )
        )
    elif source.resource_type == "OBSERVATION":
        version = await session.scalar(
            select(TaskProgressObservationModel.progress_version).where(
                TaskProgressObservationModel.organization_id == org,
                TaskProgressObservationModel.id == source.resource_id,
            )
        )
    elif source.resource_type == "WORK_LOG":
        found = await session.scalar(
            select(WorkLogModel.id).where(
                WorkLogModel.organization_id == org, WorkLogModel.id == source.resource_id
            )
        )
        version = 1 if found else None
    elif source.resource_type == "WEEKLY_BASELINE":
        row = await session.scalar(
            select(WeeklyPlanBaselineModel).where(
                WeeklyPlanBaselineModel.organization_id == org,
                WeeklyPlanBaselineModel.id == source.resource_id,
            )
        )
        version = int(row.payload["week_version"]) if row else None
    elif source.resource_type == "RISK_ASSESSMENT":
        row = await session.scalar(
            select(RiskAssessmentModel).where(
                RiskAssessmentModel.organization_id == org,
                RiskAssessmentModel.id == source.resource_id,
            )
        )
        version = int(row.payload["task_version"]) if row else None
    elif source.resource_type == "RISK_JOB":
        job = await session.scalar(
            select(RiskJobModel).where(
                RiskJobModel.organization_id == org, RiskJobModel.id == source.resource_id
            )
        )
        if job is not None:
            return "CURRENT" if job.state in ("PENDING", "RUNNING") else "UPDATED"
    elif source.resource_type == "REPORTING_WINDOW":
        row = await session.scalar(
            select(ReportingWindowModel).where(
                ReportingWindowModel.organization_id == org,
                ReportingWindowModel.id == source.resource_id,
            )
        )
        if row is not None:
            version = row.applied_version
            project_id = source.facts.get("project_id")
            at = await session.scalar(select(func.transaction_timestamp()))
            if isinstance(project_id, str) and at is not None:
                current, _ = await window_facts(
                    session,
                    actor,
                    UUID(project_id),
                    ReportingWindow.model_validate(row.payload),
                    at,
                )
                return (
                    "CURRENT"
                    if version == source.version and canonical_hash(current) == source.fingerprint
                    else "UPDATED"
                )
    elif source.resource_type == "TASK_TRANSITION":
        found = await session.scalar(
            select(AuditEventModel.id).where(
                AuditEventModel.organization_id == org, AuditEventModel.id == source.resource_id
            )
        )
        version = 1 if found else None
    if version is None:
        return "UNAVAILABLE"
    return "CURRENT" if version == source.version else "UPDATED"


async def summary_source_available(
    session: AsyncSession, org: UUID, project_id: UUID, source: SummarySource
) -> bool:
    from app.modules.progress.adapters.assessment_models import WarningAcknowledgmentModel
    from app.modules.progress.adapters.daily_update_models import DailyUpdateEvidenceLinkModel
    from app.modules.progress.adapters.evidence_models import EvidenceOriginalModel
    from app.modules.risk.adapters.database_models import RiskReviewModel

    task = await session.scalar(
        select(TaskModel.id).where(
            TaskModel.organization_id == org,
            TaskModel.id == source.task_id,
            TaskModel.project_id == project_id,
        )
    )
    if task is None:
        return False
    if source.kind == "EVIDENCE":
        link = await session.scalar(
            select(DailyUpdateEvidenceLinkModel).where(
                DailyUpdateEvidenceLinkModel.organization_id == org,
                DailyUpdateEvidenceLinkModel.id == source.id,
                DailyUpdateEvidenceLinkModel.evidence_id == source.evidence_id,
                DailyUpdateEvidenceLinkModel.evidence_version == source.evidence_version,
            )
        )
        original = await session.scalar(
            select(EvidenceOriginalModel.id).where(
                EvidenceOriginalModel.organization_id == org,
                EvidenceOriginalModel.id == source.evidence_id,
                EvidenceOriginalModel.version == source.evidence_version,
                EvidenceOriginalModel.state == "READY",
            )
        )
        return link is not None and original is not None
    if source.kind == "REVIEW":
        for model in (RiskReviewModel, WarningAcknowledgmentModel):
            if await session.scalar(
                select(model.id).where(model.organization_id == org, model.id == source.id)
            ):
                return True
        return False
    model = BlockerModel if source.kind == "BLOCKER" else RiskAssessmentModel
    return (
        await session.scalar(
            select(model.id).where(
                model.organization_id == org, model.id == source.id, model.task_id == source.task_id
            )
        )
        is not None
    )
