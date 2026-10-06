from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.reporting.domain.reports import ReportCaptureConflict, ReportError
from app.modules.reporting.domain.snapshots import canonical_hash

from ..domain.feedback import FeedbackCommand, FeedbackResult, TerminalReviewCommand
from .ports import FeedbackRepository, FeedbackTransactionFactory


class FeedbackService:
    def __init__(self, transactions: FeedbackTransactionFactory):
        self.transactions = transactions

    @staticmethod
    async def record_terminal(
        repository: FeedbackRepository, review: TerminalReviewCommand
    ) -> FeedbackResult:
        return await repository.record_terminal(review)

    async def record(
        self, *, actor: AuthenticatedActor, command: FeedbackCommand, idempotency_key: str
    ) -> FeedbackResult:
        fingerprint = canonical_hash(command.model_dump(mode="json"))
        try:
            for attempt in range(3):
                try:
                    async with self.transactions(actor) as repo:
                        return await repo.record(command, idempotency_key, fingerprint)
                except ReportCaptureConflict:
                    if attempt == 2:
                        raise ReportError("FEEDBACK_RETRY") from None
            raise ReportError("FEEDBACK_RETRY")
        except ReportError as exc:
            await self.audit_rejection(
                actor=actor, command=command, key=idempotency_key, reason_code=exc.code
            )
            raise

    async def audit_rejection(
        self,
        *,
        actor: AuthenticatedActor,
        command: FeedbackCommand | None,
        key: str | None,
        reason_code: str,
    ) -> None:
        async with self.transactions(actor) as repo:
            await repo.rejected(command, key, reason_code)
