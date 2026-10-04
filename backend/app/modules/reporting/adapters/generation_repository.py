"""Short tenant-authorized transactions for generation requests and leased proposals."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.audit.domain.events import AuditOutcome
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.planning_runs.adapters.database_models import OutboxEventModel
from app.modules.work.adapters.database_models import (
    IdempotencyRecordModel,
    IdempotencyState,
    ProjectModel,
)
from work_management_ai.agents.reporting.contracts import (
    ReportingContext,
    ReportingProposal,
    ReportingRequest,
    ReportingUsageScope,
)
from work_management_ai.agents.reporting.evaluators.grounding import verify_grounding
from work_management_ai.agents.reporting.evaluators.numeric import verify_numeric
from work_management_ai.runtime.contracts import JsonValue

from ..application.ports import GenerationRepository
from ..domain.commands import GenerateNarrativeCommand
from ..domain.generation import GenerationJob
from ..domain.narrative import FactBlock, render_fact
from ..domain.reports import ReportError, ReportResult, ReportVersion
from .database_models import ReportModel, ReportVersionModel
from .narrative_runtime import to_reporting_snapshot
from .repository import SQLReportRepository, version_domain
from .usage_models import ReportGenerationJobModel, ReportGenerationUsageModel


def job_domain(row: ReportGenerationJobModel) -> GenerationJob:
    return GenerationJob.model_validate(
        {key: getattr(row, key) for key in GenerationJob.model_fields}
    )


async def enqueue_initial(
    session: AsyncSession, *, actor: AuthenticatedActor, result: ReportResult, key: str
) -> UUID:
    job = ReportGenerationJobModel(
        id=uuid4(),
        organization_id=actor.organization_id,
        report_id=result.report.id,
        base_version_id=result.selected_version.id,
        snapshot_id=result.snapshot.id,
        snapshot_hash=result.snapshot.snapshot_hash,
        requester_membership_id=actor.membership_id,
        request_key=key,
        expected_report_version=result.report.version,
        state="QUEUED",
        created_at=cast(datetime, await session.scalar(text("SELECT clock_timestamp()"))),
    )
    session.add(job)
    await session.flush()
    session.add(
        ReportGenerationUsageModel(organization_id=actor.organization_id, generation_id=job.id)
    )
    session.add(
        OutboxEventModel(
            id=uuid4(),
            organization_id=actor.organization_id,
            event_id=uuid4(),
            event_type="report.generation.requested.v1",
            aggregate_type="report",
            aggregate_id=result.report.id,
            payload={
                "schema_version": "1.0",
                "report_id": str(result.report.id),
                "generation_id": str(job.id),
            },
            status="PENDING",
        )
    )
    return job.id


class SQLGenerationRepository:
    def __init__(self, session: AsyncSession, actor: AuthenticatedActor | UUID, timezone: str):
        self.session, self.actor = session, actor
        self.org = actor.organization_id if isinstance(actor, AuthenticatedActor) else actor
        self.reports = (
            SQLReportRepository(session, actor, timezone)
            if isinstance(actor, AuthenticatedActor)
            else None
        )

    async def authenticate(self) -> SQLReportRepository:
        if self.reports is None:
            raise ReportError("FORBIDDEN", 403)
        await self.reports.authenticate()
        return self.reports

    async def audit(
        self,
        action: str,
        resource_id: UUID | None,
        *,
        succeeded: bool,
        key: str | None = None,
        code: str | None = None,
    ) -> None:
        self.session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id
                if isinstance(self.actor, AuthenticatedActor)
                else None,
                action=action,
                resource_type="report",
                resource_id=resource_id,
                request_id=str(uuid4()),
                outcome=AuditOutcome.SUCCEEDED if succeeded else AuditOutcome.REJECTED,
                idempotency_key=key,
                before_data={},
                after_data={},
                reason_data={"reason_code": code} if code else {},
            )
        )

    async def request(
        self,
        report_id: UUID,
        command: GenerateNarrativeCommand,
        expected: int,
        key: str,
        fingerprint: str,
    ) -> ReportResult:
        reports = await self.authenticate()
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"{self.org}:report.generate:{reports.actor.membership_id}:{key}"},
        )
        row = await self.session.scalar(
            select(ReportModel)
            .where(ReportModel.organization_id == self.org, ReportModel.id == report_id)
            .with_for_update()
        )
        if row is None:
            raise ReportError("RESOURCE_NOT_FOUND", 404)
        replay = await self.session.scalar(
            select(IdempotencyRecordModel).where(
                IdempotencyRecordModel.organization_id == self.org,
                IdempotencyRecordModel.actor_membership_id == reports.actor.membership_id,
                IdempotencyRecordModel.operation == "report.generate",
                IdempotencyRecordModel.idempotency_key == key,
            )
        )
        if replay:
            if replay.request_fingerprint != fingerprint:
                raise ReportError("IDEMPOTENCY_KEY_REUSED", 409)
            return await reports.get(report_id, replayed=True)
        if row.version != expected:
            raise ReportError("STALE_REPORT_VERSION", 412)
        result = await reports.get(report_id)
        if (
            command.base_version_id != row.selected_version_id
            or command.snapshot_hash != result.snapshot.snapshot_hash
        ):
            raise ReportError("REPORT_GENERATION_MISMATCH", 409)
        active = await self.session.scalar(
            select(ReportGenerationJobModel.id).where(
                ReportGenerationJobModel.organization_id == self.org,
                ReportGenerationJobModel.report_id == report_id,
                ReportGenerationJobModel.state.in_(("QUEUED", "RUNNING")),
            )
        )
        if active:
            raise ReportError("REPORT_GENERATION_PENDING", 409)
        row.version += 1
        row.narrative_requested = True
        result = result.model_copy(
            update={
                "report": result.report.model_copy(
                    update={"version": row.version, "narrative_requested": True}
                )
            }
        )
        await enqueue_initial(self.session, actor=reports.actor, result=result, key=key)
        at = await reports.captured_at()
        self.session.add(
            IdempotencyRecordModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=reports.actor.membership_id,
                operation="report.generate",
                idempotency_key=key,
                request_fingerprint=fingerprint,
                state=IdempotencyState.COMPLETED,
                response_status=202,
                response_body={"report_id": str(report_id)},
                expires_at=at + timedelta(days=7),
            )
        )
        await self.audit("report.generation.requested", report_id, succeeded=True, key=key)
        await self.session.flush()
        return await reports.get(report_id)

    async def job(self, scope: ReportingUsageScope) -> ReportGenerationJobModel:
        row = await self.session.scalar(
            select(ReportGenerationJobModel)
            .where(
                ReportGenerationJobModel.organization_id == self.org,
                ReportGenerationJobModel.id == scope.generation_id,
            )
            .with_for_update()
        )
        if scope.organization_id != self.org or (
            isinstance(self.actor, AuthenticatedActor)
            and self.actor.membership_id != scope.membership_id
        ):
            raise ReportError("REPORT_GENERATION_SCOPE", 403)
        if row is None or row.requester_membership_id != scope.membership_id:
            raise ReportError("REPORT_GENERATION_SCOPE", 403)
        at = cast(datetime, await self.session.scalar(text("SELECT clock_timestamp()")))
        if (
            row.state != "RUNNING"
            or row.fence != scope.fence
            or row.lease_owner != scope.worker_id
            or row.lease_until is None
            or row.lease_until <= at
            or row.deadline is None
            or row.deadline <= at
        ):
            raise ReportError("REPORT_GENERATION_LEASE", 409)
        return row

    async def context(
        self, request: ReportingRequest, scope: ReportingUsageScope
    ) -> ReportingContext:
        reports = await self.authenticate()
        row = await self.job(scope)
        result = await reports.get(request.report_id)
        if (
            row.report_id != request.report_id
            or row.base_version_id != request.base_version_id
            or row.request_key != request.request_key
            or row.snapshot_hash != request.snapshot_hash
            or result.report.version != row.expected_report_version
            or result.report.selected_version_id != row.base_version_id
            or request.locale != result.report.locale
        ):
            raise ReportError("STALE_REPORT_VERSION", 412)
        label = await self.session.scalar(
            select(ProjectModel.name).where(
                ProjectModel.organization_id == self.org,
                ProjectModel.id == result.report.project_id,
            )
        )
        if label is None:
            raise ReportError("RESOURCE_NOT_FOUND", 404)
        return ReportingContext(
            snapshot=to_reporting_snapshot(result.snapshot),
            base_version_id=row.base_version_id,
            locale=result.report.locale,
            project_label=label,
        )

    async def store(self, proposal: ReportingProposal, scope: ReportingUsageScope) -> ReportVersion:
        reports = await self.authenticate()
        job = await self.job(scope)
        if job.proposed_version_id:
            row = await self.session.scalar(
                select(ReportVersionModel).where(
                    ReportVersionModel.organization_id == self.org,
                    ReportVersionModel.id == job.proposed_version_id,
                )
            )
            if row is None:
                raise ReportError("REPORT_GENERATION_FAILED")
            return version_domain(row)
        context = await self.context(proposal.request, scope)
        numeric = verify_numeric(context.snapshot, proposal.narrative, source_detail_limit=100)
        semantic = verify_grounding(proposal.narrative, proposal.semantic_verdict)
        if not numeric.passed or not semantic.passed or proposal.narrative.locale != context.locale:
            raise ReportError("REPORT_VERIFICATION_FAILED")
        row = await self.session.scalar(
            select(ReportModel)
            .where(ReportModel.organization_id == self.org, ReportModel.id == job.report_id)
            .with_for_update()
        )
        if (
            row is None
            or row.version != job.expected_report_version
            or row.selected_version_id != job.base_version_id
        ):
            raise ReportError("STALE_REPORT_VERSION", 412)
        result = await reports.get(row.id)
        version = ReportVersion(
            id=uuid4(),
            report_id=row.id,
            snapshot_id=job.snapshot_id,
            origin="AI_PROPOSED",
            locale=context.locale,
            created_at=await reports.captured_at(),
            narrative=proposal.narrative,
            rendered_facts={
                b.id: render_fact(b, result.snapshot, context.locale)
                for b in proposal.narrative.blocks
                if isinstance(b, FactBlock)
            },
            provenance=cast(
                dict[str, JsonValue],
                {
                    **proposal.model_dump(mode="json", exclude={"request", "narrative"}),
                    "numeric_verdict": numeric.model_dump(mode="json"),
                    "generation_id": str(job.id),
                    "orchestration_run_id": str(job.orchestration_run_id),
                },
            ),
            generation_id=job.id,
            base_version_id=job.base_version_id,
        )
        self.session.add(
            ReportVersionModel(
                id=version.id,
                organization_id=self.org,
                report_id=row.id,
                snapshot_id=job.snapshot_id,
                origin="AI_PROPOSED",
                locale=version.locale,
                created_at=version.created_at,
                payload=version.model_dump(mode="json"),
            )
        )
        await self.session.flush()
        row.selected_version_id = version.id
        row.version += 1
        job.proposed_version_id = version.id
        await self.audit("report.narrative.proposed", row.id, succeeded=True, key=job.request_key)
        await self.session.flush()
        return version

    async def recoverable(self) -> GenerationJob | None:
        row = await self.session.scalar(
            select(ReportGenerationJobModel)
            .where(
                ReportGenerationJobModel.organization_id == self.org,
                ReportGenerationJobModel.state == "RUNNING",
                ReportGenerationJobModel.proposed_version_id.is_not(None),
                ReportGenerationJobModel.lease_until < text("clock_timestamp()"),
            )
            .order_by(ReportGenerationJobModel.created_at, ReportGenerationJobModel.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        return job_domain(row) if row else None

    async def reconcile(self, job: GenerationJob, *, succeeded: bool) -> None:
        row = await self.session.scalar(
            select(ReportGenerationJobModel)
            .where(
                ReportGenerationJobModel.organization_id == self.org,
                ReportGenerationJobModel.id == job.id,
            )
            .with_for_update()
        )
        at = cast(datetime, await self.session.scalar(text("SELECT clock_timestamp()")))
        if (
            row is not None
            and row.state in ("AWAITING_REVIEW", "AI_UNAVAILABLE")
            and row.fence == job.fence
            and row.proposed_version_id == job.proposed_version_id
        ):
            return
        if (
            row is None
            or row.state != "RUNNING"
            or row.fence != job.fence
            or row.proposed_version_id != job.proposed_version_id
            or row.lease_until is None
            or row.lease_until > at
        ):
            raise ReportError("REPORT_GENERATION_LEASE", 409)
        row.state = "AWAITING_REVIEW" if succeeded else "AI_UNAVAILABLE"
        row.safe_error_code = None if succeeded else "REPORTING_UNAVAILABLE"
        row.lease_owner = None
        row.lease_until = None
        await self.audit(
            "report.generation.reconciled",
            row.report_id,
            succeeded=succeeded,
            code=row.safe_error_code,
        )

    async def claim(self, worker_id: str) -> GenerationJob | None:
        row = await self.session.scalar(
            select(ReportGenerationJobModel)
            .where(
                ReportGenerationJobModel.organization_id == self.org,
                ReportGenerationJobModel.proposed_version_id.is_(None),
                (ReportGenerationJobModel.state == "QUEUED")
                | (
                    (ReportGenerationJobModel.state == "RUNNING")
                    & (ReportGenerationJobModel.lease_until < text("clock_timestamp()"))
                ),
            )
            .order_by(ReportGenerationJobModel.created_at, ReportGenerationJobModel.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        at = cast(datetime, await self.session.scalar(text("SELECT clock_timestamp()")))
        if row.claims >= 3 or (row.deadline is not None and row.deadline <= at):
            row.state = "AI_UNAVAILABLE"
            row.safe_error_code = "REPORT_GENERATION_EXHAUSTED"
            row.lease_owner = None
            row.lease_until = None
            await self.audit(
                "report.generation.failed", row.report_id, succeeded=False, code=row.safe_error_code
            )
            return None
        row.claims += 1
        row.fence += 1
        row.state = "RUNNING"
        row.lease_owner = worker_id
        row.started_at = row.started_at or at
        deadline = row.deadline or (at + timedelta(seconds=180))
        row.deadline = deadline
        row.lease_until = min(deadline, at + timedelta(seconds=30))
        await self.session.flush()
        return job_domain(row)

    async def heartbeat(self, scope: ReportingUsageScope) -> None:
        row = await self.job(scope)
        at = cast(datetime, await self.session.scalar(text("SELECT clock_timestamp()")))
        assert row.deadline is not None
        row.lease_until = min(row.deadline, at + timedelta(seconds=30))

    async def attach_run(self, scope: ReportingUsageScope, run_id: UUID) -> None:
        row = await self.job(scope)
        if row.orchestration_run_id and row.orchestration_run_id != run_id:
            raise ReportError("REPORT_GENERATION_RUN_MISMATCH")
        row.orchestration_run_id = run_id

    async def complete(
        self, scope: ReportingUsageScope, *, succeeded: bool, code: str | None = None
    ) -> None:
        row = await self.job(scope)
        row.state = (
            "AWAITING_REVIEW"
            if succeeded and row.proposed_version_id
            else "FAILED"
            if code == "REPORTING_JOB_FAILED"
            else "AI_UNAVAILABLE"
        )
        row.safe_error_code = code if row.state != "AWAITING_REVIEW" else None
        row.lease_owner = None
        row.lease_until = None
        await self.audit(
            "report.generation.completed"
            if row.state == "AWAITING_REVIEW"
            else "report.generation.failed",
            row.report_id,
            succeeded=row.state == "AWAITING_REVIEW",
            code=row.safe_error_code,
        )


class GenerationTransactions:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], timezone: str):
        self.sessions, self.timezone = sessions, timezone

    @asynccontextmanager
    async def __call__(
        self, actor: AuthenticatedActor | UUID
    ) -> AsyncGenerator[GenerationRepository]:
        async with self.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true),"
                    "set_config('app.membership_id',:member,true)"
                ),
                {
                    "org": str(
                        actor.organization_id if isinstance(actor, AuthenticatedActor) else actor
                    ),
                    "member": str(actor.membership_id)
                    if isinstance(actor, AuthenticatedActor)
                    else "",
                },
            )
            yield cast(GenerationRepository, SQLGenerationRepository(session, actor, self.timezone))
