"""Append sourced outcomes in an authorized tenant transaction; never rewrite feedback."""

from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.reporting.domain.reports import ReportCaptureConflict, ReportError
from app.modules.reporting.domain.snapshots import canonical_hash

from ..domain.outcomes import FeedbackOutcome, OutcomeSourceCommand
from .ports import FeedbackTransactionFactory


class OutcomeService:
    def __init__(self, transactions: FeedbackTransactionFactory):
        self.transactions = transactions

    async def record(
        self,
        *,
        actor: AuthenticatedActor,
        feedback_id: UUID,
        source: OutcomeSourceCommand,
        idempotency_key: str,
    ) -> FeedbackOutcome:
        fingerprint = canonical_hash(
            {"feedback_id": str(feedback_id), "source": source.model_dump(mode="json")}
        )
        try:
            if not 16 <= len(idempotency_key) <= 128:
                raise ReportError("VALIDATION_FAILED", 422)
            for attempt in range(3):
                try:
                    async with self.transactions(actor) as repo:
                        return await repo.record_outcome(
                            feedback_id, source, idempotency_key, fingerprint
                        )
                except ReportCaptureConflict:
                    if attempt == 2:
                        raise ReportError("FEEDBACK_RETRY") from None
            raise ReportError("FEEDBACK_RETRY")
        except ReportError as exc:
            await self.audit_rejection(
                actor=actor, feedback_id=feedback_id, key=idempotency_key, reason_code=exc.code
            )
            raise

    async def audit_rejection(
        self,
        *,
        actor: AuthenticatedActor,
        feedback_id: UUID | None,
        key: str | None,
        reason_code: str,
    ) -> None:
        async with self.transactions(actor) as repo:
            await repo.reject_outcome(feedback_id, key, reason_code)
