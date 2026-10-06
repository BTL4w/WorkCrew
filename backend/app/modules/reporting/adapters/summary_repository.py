"""Tenant-qualified, serialized summary conversion and live scheduler authorization."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.assistant.adapters.agent_runtime import CurrentActorResolverPort
from app.modules.automations.adapters.delivery_models import SummarySnapshotModel
from app.modules.automations.adapters.digest_repository import DigestRepository
from app.modules.automations.domain.digests import DailySummarySnapshot
from app.modules.automations.domain.schedules import ScheduleError
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.planning_runs.domain.models import OutboxEvent

from ..application.summary_report_service import SummaryReportRepository, SummaryReportService
from ..domain.commands import CreateReportCommand
from ..domain.periods import ReportKind
from ..domain.reports import ReportCaptureConflict, ReportError, ReportResult
from ..domain.snapshots import ReportMetricSnapshot, canonical_hash
from .database_models import ReportModel
from .generation_repository import enqueue_initial
from .repository import SQLReportRepository


async def authorize_summary(
    session: AsyncSession, actor: AuthenticatedActor, summary_id: UUID, *, lock: bool = False
) -> DailySummarySnapshot:
    query = select(SummarySnapshotModel).where(
        SummarySnapshotModel.organization_id == actor.organization_id,
        SummarySnapshotModel.id == summary_id,
    )
    if lock:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"summary-report:{actor.organization_id}:{summary_id}"},
        )
    stored = await session.scalar(query)
    if stored is None:
        raise ReportError("RESOURCE_NOT_FOUND", 404)
    summary = DailySummarySnapshot.model_validate(stored.payload)
    digests = DigestRepository(session, actor)
    await digests.authenticate()
    await digests.authorize(summary.project_id)
    current = await digests.by_id(summary.schedule_id)
    applied = await digests.applied(current, summary.window.applied_version)
    allowed = {r.membership_id for r in await digests.recipients(summary.project_id)}
    if (
        current.paused
        or current.narrative_mode != "DRAFT_FOR_MANAGER"
        or applied.narrative_mode != "DRAFT_FOR_MANAGER"
        or applied.creator_membership_id != actor.membership_id
        or not set(current.recipients) & set(applied.recipients) & allowed
    ):
        raise ReportError("SUMMARY_NARRATIVE_CANCELLED")
    return summary


class SQLSummaryReportRepository(SQLReportRepository):
    async def summary(self, summary_id: UUID) -> DailySummarySnapshot:
        try:
            return await authorize_summary(self.session, self.actor, summary_id, lock=True)
        except ScheduleError as exc:
            raise ReportError(exc.code, exc.status) from exc

    async def existing_summary(
        self, summary_id: UUID, locale: str, workflow: str
    ) -> ReportResult | None:
        id = await self.session.scalar(
            select(ReportModel.id).where(
                ReportModel.organization_id == self.org,
                ReportModel.summary_id == summary_id,
                ReportModel.locale == locale,
                ReportModel.workflow_version == workflow,
            )
        )
        return await self.get(id) if id else None

    async def save_summary(
        self,
        snapshot: ReportMetricSnapshot,
        summary: DailySummarySnapshot,
        locale: Literal["vi", "en"],
        workflow: str,
    ) -> ReportResult:
        key = f"summary:{canonical_hash([str(summary.id), locale, workflow])}"
        result = await self.save(
            snapshot,
            CreateReportCommand(
                project_id=summary.project_id,
                kind=ReportKind.DAILY,
                period_start=snapshot.period.local_start,
                timezone=snapshot.period.timezone,
                locale=locale,
                narrative_enabled=False,
            ),
            uuid4(),
            key,
            snapshot.snapshot_hash,
            key,
        )
        row = await self.session.get(ReportModel, result.report.id)
        assert row is not None
        row.origin = "DAILY_SUMMARY"
        row.summary_id = summary.id
        row.summary_hash = canonical_hash(summary.model_dump(mode="json"))
        row.workflow_version = workflow
        row.narrative_requested = True
        await self.session.flush()
        result = await self.get(row.id)
        await enqueue_initial(self.session, actor=self.actor, result=result, key=key)
        return await self.get(row.id)


class SummaryReportTransactions:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], timezone: str):
        self.sessions, self.timezone = sessions, timezone

    @asynccontextmanager
    async def __call__(self, actor: AuthenticatedActor) -> AsyncGenerator[SummaryReportRepository]:
        try:
            async with self.sessions() as session, session.begin():
                await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                await session.execute(text("SET LOCAL ROLE app_runtime"))
                await session.execute(
                    text(
                        "SELECT set_config('app.organization_id',:org,true),"
                        "set_config('app.membership_id',:member,true)"
                    ),
                    {"org": str(actor.organization_id), "member": str(actor.membership_id)},
                )
                yield SQLSummaryReportRepository(session, actor, self.timezone)
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) in {"40001", "40P01", "23505"}:
                raise ReportCaptureConflict() from exc
            raise


class SummaryReportingPublisher:
    """Event metadata resolves the original actor; application policy remains authoritative."""

    def __init__(self, service: SummaryReportService, actors: CurrentActorResolverPort):
        self.service, self.actors = service, actors

    async def publish(self, event: OutboxEvent) -> None:
        from app.modules.automations.domain.digests import SummaryCaptured
        from app.modules.organization.domain.roles import MembershipRole

        value = SummaryCaptured.model_validate(event.payload)
        if (
            event.envelope_version != "1.0"
            or event.aggregate_type != "daily_summary"
            or value.summary_id != event.aggregate_id
        ):
            raise ValueError("INVALID_SUMMARY_EVENT")
        if value.narrative_mode == "NONE" or value.creator_membership_id is None:
            return
        actor = await self.actors.resolve(
            organization_id=event.organization_id, membership_id=value.creator_membership_id
        )
        if actor is None or actor.role not in (MembershipRole.MANAGER, MembershipRole.ADMIN):
            return
        try:
            await self.service.ensure_draft(
                actor=actor,
                summary_id=value.summary_id,
                locale=value.narrative_locale,
                workflow_version="reporting-narrative.v1",
            )
        except ReportError as exc:
            if exc.code not in {"SUMMARY_NARRATIVE_CANCELLED", "FORBIDDEN", "RESOURCE_NOT_FOUND"}:
                raise
