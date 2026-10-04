"""Human request and specialist proposal transaction boundaries."""

from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from work_management_ai.agents.reporting.contracts import (
    ReportingContext,
    ReportingRequest,
    ReportingUsageScope,
)

from ..domain.commands import GenerateNarrativeCommand, StoreNarrativeCommand
from ..domain.reports import ReportError, ReportResult, ReportVersion
from ..domain.snapshots import canonical_hash
from .ports import GenerationTransactionFactory


class GenerationService:
    def __init__(self, transactions: GenerationTransactionFactory):
        self.transactions = transactions

    async def request(
        self,
        *,
        actor: AuthenticatedActor,
        report_id: UUID,
        command: GenerateNarrativeCommand,
        expected_version: int,
        idempotency_key: str,
    ) -> ReportResult:
        fingerprint = canonical_hash(
            {
                "report_id": str(report_id),
                "command": command.model_dump(mode="json"),
                "version": expected_version,
            }
        )
        try:
            async with self.transactions(actor) as repo:
                return await repo.request(
                    report_id, command, expected_version, idempotency_key, fingerprint
                )
        except ReportError as exc:
            await self.audit_rejection(
                actor=actor, report_id=report_id, key=idempotency_key, reason_code=exc.code
            )
            raise

    async def audit_rejection(
        self,
        *,
        actor: AuthenticatedActor,
        report_id: UUID | None,
        key: str | None,
        reason_code: str,
    ) -> None:
        async with self.transactions(actor) as repo:
            await repo.audit(
                "report.generation.requested", report_id, succeeded=False, key=key, code=reason_code
            )

    async def read_context(
        self, *, actor: AuthenticatedActor, request: ReportingRequest, scope: ReportingUsageScope
    ) -> ReportingContext:
        async with self.transactions(actor) as repo:
            return await repo.context(request, scope)

    async def store_proposal(
        self, *, actor: AuthenticatedActor, command: StoreNarrativeCommand
    ) -> ReportVersion:
        proposal, scope = command.proposal, command.scope
        try:
            async with self.transactions(actor) as repo:
                return await repo.store(proposal, scope)
        except ReportError as exc:
            async with self.transactions(actor) as repo:
                await repo.audit(
                    "report.narrative.proposed",
                    proposal.request.report_id,
                    succeeded=False,
                    key=proposal.request.request_key,
                    code=exc.code,
                )
            raise
