"""Transactional report creation, reads and current-authority replay."""

from datetime import timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from app.modules.identity.domain.auth import AuthenticatedActor

from ..domain.commands import CaptureReportCommand, CreateReportCommand
from ..domain.periods import ReportKind, normalize_period
from ..domain.reports import (
    ReportCaptureConflict,
    ReportDefaults,
    ReportError,
    ReportPage,
    ReportResult,
    ReportSourcePage,
)
from ..domain.snapshots import canonical_hash
from .ports import ReportTransactionFactory
from .snapshot_service import ReportSnapshotService


class ReportService:
    def __init__(self, transactions: ReportTransactionFactory):
        self.transactions = transactions

    async def create(
        self,
        *,
        actor: AuthenticatedActor,
        command: CreateReportCommand,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> ReportResult:
        request_id = request_id or str(uuid4())
        fingerprint = canonical_hash(command.model_dump(mode="json"))
        try:
            for attempt in range(3):
                try:
                    async with self.transactions(actor) as repo:
                        await repo.authenticate()
                        await repo.authorize_project(command.project_id)
                        replay = await repo.replay(idempotency_key, fingerprint)
                        if replay is not None:
                            return await repo.get(replay, replayed=True)
                        at = await repo.captured_at()
                        timezone = (
                            command.timezone
                            if command.timezone is not None
                            else await repo.timezone(command.project_id)
                        )
                        try:
                            start = command.period_start or at.astimezone(ZoneInfo(timezone)).date()
                            if command.period_start is None and command.kind is ReportKind.WEEKLY:
                                start -= timedelta(days=start.weekday())
                            period = normalize_period(command.kind, start, timezone, at)
                        except (ValueError, KeyError) as exc:
                            raise ReportError("VALIDATION_FAILED", 422) from exc
                        snapshot = await ReportSnapshotService(repo.snapshot_reader).capture(
                            CaptureReportCommand(
                                report_id=uuid4(),
                                snapshot_id=uuid4(),
                                project_id=command.project_id,
                                captured_at=at,
                                period=period,
                            )
                        )
                        return await repo.save(
                            snapshot, command, uuid4(), idempotency_key, fingerprint, request_id
                        )
                except ReportCaptureConflict:
                    if attempt == 2:
                        raise ReportError("REPORT_CAPTURE_RETRY", 409) from None
            raise ReportError("REPORT_CAPTURE_RETRY", 409)
        except ReportError as exc:
            async with self.transactions(actor) as repo:
                await repo.audit_rejection(
                    request_id, idempotency_key, exc.code, command.project_id
                )
            raise

    async def get(self, *, actor: AuthenticatedActor, report_id: UUID) -> ReportResult:
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            return await repo.get(report_id)

    async def list(
        self, *, actor: AuthenticatedActor, project_id: UUID, page: int = 1, page_size: int = 20
    ) -> ReportPage:
        if page < 1 or not 1 <= page_size <= 100:
            raise ReportError("VALIDATION_FAILED", 422)
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            return await repo.list(project_id, page, page_size)

    async def defaults(self, *, actor: AuthenticatedActor, project_id: UUID) -> ReportDefaults:
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            await repo.authorize_project(project_id)
            timezone = await repo.timezone(project_id)
            at = await repo.captured_at()
            return ReportDefaults(
                timezone=timezone, period_start=at.astimezone(ZoneInfo(timezone)).date()
            )

    async def sources(
        self,
        *,
        actor: AuthenticatedActor,
        report_id: UUID,
        cursor: str | None = None,
        page_size: int = 20,
    ) -> ReportSourcePage:
        if not 1 <= page_size <= 100:
            raise ReportError("VALIDATION_FAILED", 422)
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            return await repo.sources(report_id, cursor, page_size)
