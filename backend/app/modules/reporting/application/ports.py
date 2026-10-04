"""Report application-owned SQL and transaction ports."""

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor

from ..domain.commands import CaptureReportCommand, CreateReportCommand, PublishReportCommand
from ..domain.reports import ReportPage, ReportResult, ReportSourcePage
from ..domain.snapshots import ReportMetricSnapshot


class ReportSnapshotReadPort(Protocol):
    async def capture(self, command: CaptureReportCommand) -> ReportMetricSnapshot: ...


class ReportRepository(Protocol):
    snapshot_reader: ReportSnapshotReadPort

    async def authenticate(self) -> None: ...
    async def authorize_project(self, project_id: UUID) -> None: ...
    async def captured_at(self) -> datetime: ...
    async def timezone(self, project_id: UUID) -> str: ...
    async def replay(self, key: str, fingerprint: str) -> UUID | None: ...
    async def save(
        self,
        snapshot: ReportMetricSnapshot,
        command: CreateReportCommand,
        version_id: UUID,
        key: str,
        fingerprint: str,
        request_id: str,
    ) -> ReportResult: ...
    async def publish(
        self,
        report_id: UUID,
        command: PublishReportCommand,
        expected_version: int,
        key: str,
        fingerprint: str,
        request_id: str,
    ) -> ReportResult: ...
    async def audit_publish_rejection(
        self, request_id: str, key: str | None, code: str, report_id: UUID | None
    ) -> None: ...
    async def get(self, report_id: UUID, *, replayed: bool = False) -> ReportResult: ...
    async def list(self, project_id: UUID, page: int, page_size: int) -> ReportPage: ...
    async def sources(
        self, report_id: UUID, cursor: str | None, page_size: int
    ) -> ReportSourcePage: ...
    async def audit_rejection(
        self, request_id: str, key: str | None, code: str, project_id: UUID | None
    ) -> None: ...


class ReportTransactionFactory(Protocol):
    def __call__(
        self, actor: AuthenticatedActor
    ) -> AbstractAsyncContextManager[ReportRepository]: ...
