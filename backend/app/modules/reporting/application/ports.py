"""Report application-owned SQL and transaction ports."""

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from work_management_ai.agents.reporting.contracts import (
    ReportingContext,
    ReportingProposal,
    ReportingRequest,
    ReportingUsageScope,
)

from ..domain.commands import (
    CaptureReportCommand,
    CreateReportCommand,
    GenerateNarrativeCommand,
    PublishReportCommand,
)
from ..domain.generation import GenerationJob
from ..domain.reports import ReportPage, ReportResult, ReportSourcePage, ReportVersion
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


class GenerationRepository(Protocol):
    async def request(
        self,
        report_id: UUID,
        command: GenerateNarrativeCommand,
        expected: int,
        key: str,
        fingerprint: str,
    ) -> ReportResult: ...
    async def context(
        self, request: ReportingRequest, scope: ReportingUsageScope
    ) -> ReportingContext: ...
    async def store(
        self, proposal: ReportingProposal, scope: ReportingUsageScope
    ) -> ReportVersion: ...
    async def audit(
        self,
        action: str,
        resource_id: UUID | None,
        *,
        succeeded: bool,
        key: str | None = None,
        code: str | None = None,
    ) -> None: ...
    async def recoverable(self) -> GenerationJob | None: ...
    async def reconcile(self, job: GenerationJob, *, succeeded: bool) -> None: ...
    async def claim(self, worker_id: str) -> GenerationJob | None: ...
    async def heartbeat(self, scope: ReportingUsageScope) -> None: ...
    async def complete(
        self, scope: ReportingUsageScope, *, succeeded: bool, code: str | None = None
    ) -> None: ...
    async def attach_run(self, scope: ReportingUsageScope, run_id: UUID) -> None: ...


class GenerationTransactionFactory(Protocol):
    def __call__(
        self, actor: AuthenticatedActor | UUID
    ) -> AbstractAsyncContextManager[GenerationRepository]: ...
