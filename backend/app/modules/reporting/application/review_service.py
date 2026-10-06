"""Human review owns the transaction; a standalone feedback call cannot approve."""

from contextlib import AbstractAsyncContextManager
from typing import Protocol, cast
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor

from ..domain.commands import (
    EditReportCommand,
    PublishReportCommand,
    RejectReportCommand,
    ReportEditVerificationCommand,
)
from ..domain.reports import ReportCaptureConflict, ReportError, ReportResult, ReviewResult
from ..domain.snapshots import canonical_hash


class ReviewRepository(Protocol):
    async def mutate(
        self,
        operation: str,
        report_id: UUID,
        command: EditReportCommand
        | PublishReportCommand
        | RejectReportCommand
        | ReportEditVerificationCommand,
        expected: int,
        key: str,
        fingerprint: str,
    ) -> ReportResult | ReviewResult: ...
    async def audit_review_rejection(
        self, operation: str, report_id: UUID | None, key: str | None, code: str
    ) -> None: ...


class ReviewTransactionFactory(Protocol):
    def __call__(
        self, actor: AuthenticatedActor
    ) -> AbstractAsyncContextManager[ReviewRepository]: ...


class ReviewService:
    def __init__(self, transactions: ReviewTransactionFactory):
        self.transactions = transactions

    async def _mutate(
        self,
        operation: str,
        actor: AuthenticatedActor,
        report_id: UUID,
        command: EditReportCommand
        | PublishReportCommand
        | RejectReportCommand
        | ReportEditVerificationCommand,
        expected_version: int,
        idempotency_key: str,
    ) -> ReportResult | ReviewResult:
        fingerprint = canonical_hash(
            {
                "report_id": str(report_id),
                "command": command.model_dump(mode="json"),
                (
                    "expected_version" if operation == "report.publish" else "version"
                ): expected_version,
            }
        )
        try:
            for attempt in range(3):
                try:
                    async with self.transactions(actor) as repo:
                        return await repo.mutate(
                            operation,
                            report_id,
                            command,
                            expected_version,
                            idempotency_key,
                            fingerprint,
                        )
                except ReportCaptureConflict:
                    if attempt == 2:
                        raise ReportError("REPORT_REVIEW_RETRY") from None
            raise ReportError("REPORT_REVIEW_RETRY")
        except ReportError as exc:
            await self.audit_rejection(
                actor=actor,
                operation=operation,
                report_id=report_id,
                key=idempotency_key,
                reason_code=exc.code,
            )
            raise

    async def edit(
        self,
        *,
        actor: AuthenticatedActor,
        report_id: UUID,
        command: EditReportCommand,
        expected_version: int,
        idempotency_key: str,
    ) -> ReportResult:
        return cast(
            ReportResult,
            await self._mutate(
                "report.edit", actor, report_id, command, expected_version, idempotency_key
            ),
        )

    async def publish(
        self,
        *,
        actor: AuthenticatedActor,
        report_id: UUID,
        command: PublishReportCommand,
        expected_version: int,
        idempotency_key: str,
    ) -> ReviewResult:
        return cast(
            ReviewResult,
            await self._mutate(
                "report.publish",
                actor,
                report_id,
                command,
                expected_version,
                idempotency_key,
            ),
        )

    async def reject(
        self,
        *,
        actor: AuthenticatedActor,
        report_id: UUID,
        command: RejectReportCommand,
        expected_version: int,
        idempotency_key: str,
    ) -> ReviewResult:
        return cast(
            ReviewResult,
            await self._mutate(
                "report.reject", actor, report_id, command, expected_version, idempotency_key
            ),
        )

    async def audit_rejection(
        self,
        *,
        actor: AuthenticatedActor,
        operation: str,
        report_id: UUID | None,
        key: str | None,
        reason_code: str,
    ) -> None:
        async with self.transactions(actor) as repo:
            await repo.audit_review_rejection(operation, report_id, key, reason_code)

    async def request_verification(
        self,
        *,
        actor: AuthenticatedActor,
        command: ReportEditVerificationCommand,
        expected_version: int,
        idempotency_key: str,
    ) -> ReportResult:
        return cast(
            ReportResult,
            await self._mutate(
                "report.edit.verify",
                actor,
                command.report_id,
                command,
                expected_version,
                idempotency_key,
            ),
        )
