"""Authorized report feedback projection and deterministic Project quality rates."""

from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.progress.adapters.blocker_models import BlockerModel, BlockerTransitionModel
from app.modules.reporting.adapters.database_models import (
    ReportModel,
    ReportPublicationModel,
    ReportVersionModel,
)
from app.modules.reporting.adapters.usage_models import ReportGenerationJobModel
from app.modules.work.adapters.database_models import TaskModel, TaskStatusTransitionModel

from ..domain.feedback import Feedback
from ..domain.outcomes import FeedbackOutcome, OutcomeSourceCommand, review_rates
from .database_models import FeedbackModel, FeedbackOutcomeModel


def feedback_domain(row: FeedbackModel) -> Feedback:
    return Feedback.model_validate({key: getattr(row, key) for key in Feedback.model_fields})


def outcome_domain(row: FeedbackOutcomeModel) -> FeedbackOutcome:
    return FeedbackOutcome.model_validate(
        {
            **{key: getattr(row, key) for key in FeedbackOutcome.model_fields if key != "source"},
            "source": OutcomeSourceCommand.model_validate(
                {
                    "source_type": row.source_type,
                    "source_id": row.source_id,
                    "source_version": row.source_version,
                }
            ),
        }
    )


async def outcome_accessible(
    session: AsyncSession, org: UUID, project_id: UUID, row: FeedbackOutcomeModel
) -> bool:
    if row.source_type == "TASK_TRANSITION":
        task_id = await session.scalar(
            select(TaskStatusTransitionModel.task_id).where(
                TaskStatusTransitionModel.organization_id == org,
                TaskStatusTransitionModel.id == row.source_id,
            )
        )
    elif row.source_type == "TASK_ACTUALS":
        task_id = row.source_id
    else:
        task_id = await session.scalar(
            select(BlockerModel.task_id)
            .join(
                BlockerTransitionModel,
                (BlockerTransitionModel.organization_id == BlockerModel.organization_id)
                & (BlockerTransitionModel.blocker_id == BlockerModel.id),
            )
            .where(
                BlockerTransitionModel.organization_id == org,
                BlockerTransitionModel.id == row.source_id,
            )
        )
    return (
        await session.scalar(
            select(TaskModel.id).where(
                TaskModel.organization_id == org,
                TaskModel.id == task_id,
                TaskModel.project_id == project_id,
            )
        )
        is not None
    )


async def project_rates(session: AsyncSession, org: UUID, project_id: UUID):
    rows = (
        await session.scalars(
            select(FeedbackModel)
            .join(
                ReportModel,
                (FeedbackModel.organization_id == ReportModel.organization_id)
                & (FeedbackModel.report_id == ReportModel.id),
            )
            .where(
                FeedbackModel.organization_id == org,
                ReportModel.project_id == project_id,
                FeedbackModel.kind == "TERMINAL_QUALITY",
            )
        )
    ).all()
    terminal = select(FeedbackModel.generation_id).where(
        FeedbackModel.organization_id == org, FeedbackModel.kind == "TERMINAL_QUALITY"
    )
    jobs = (
        await session.execute(
            select(ReportGenerationJobModel.state, func.count())
            .join(
                ReportModel,
                (ReportGenerationJobModel.organization_id == ReportModel.organization_id)
                & (ReportGenerationJobModel.report_id == ReportModel.id),
            )
            .where(
                ReportGenerationJobModel.organization_id == org,
                ReportModel.project_id == project_id,
                ReportGenerationJobModel.job_type == "DRAFT",
                ReportGenerationJobModel.id.not_in(terminal),
            )
            .group_by(ReportGenerationJobModel.state)
        )
    ).all()
    counts = {state: count for state, count in jobs}
    published_metrics = (
        select(ReportPublicationModel.report_id)
        .join(
            ReportVersionModel,
            (ReportVersionModel.organization_id == ReportPublicationModel.organization_id)
            & (ReportVersionModel.id == ReportPublicationModel.report_version_id),
        )
        .where(
            ReportPublicationModel.organization_id == org,
            ReportVersionModel.origin == "METRICS_ONLY",
        )
    )
    manual = await session.scalar(
        select(func.count())
        .select_from(ReportModel)
        .where(
            ReportModel.organization_id == org,
            ReportModel.project_id == project_id,
            or_(ReportModel.narrative_requested.is_(False), ReportModel.id.in_(published_metrics)),
        )
    )
    return review_rates(
        (feedback_domain(row) for row in rows),
        pending=sum(counts.get(state, 0) for state in ("QUEUED", "RUNNING", "AWAITING_REVIEW")),
        failed=sum(counts.get(state, 0) for state in ("FAILED", "AI_UNAVAILABLE")),
        manual=manual or 0,
    )


async def report_feedback(session: AsyncSession, org: UUID, report_id: UUID, project_id: UUID):
    feedback = (
        await session.scalars(
            select(FeedbackModel)
            .where(FeedbackModel.organization_id == org, FeedbackModel.report_id == report_id)
            .order_by(FeedbackModel.created_at, FeedbackModel.id)
        )
    ).all()
    outcomes = (
        await session.scalars(
            select(FeedbackOutcomeModel)
            .where(
                FeedbackOutcomeModel.organization_id == org,
                FeedbackOutcomeModel.feedback_id.in_([row.id for row in feedback]),
            )
            .order_by(FeedbackOutcomeModel.recorded_at, FeedbackOutcomeModel.id)
        )
    ).all()
    visible = [
        outcome_domain(row)
        for row in outcomes
        if await outcome_accessible(session, org, project_id, row)
    ]
    return tuple(feedback_domain(row) for row in feedback), tuple(visible)
