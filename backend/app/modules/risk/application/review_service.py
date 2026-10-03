"""Manual append-only dispositions, never a score override."""

from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.risk.application.ports import RiskTransactionPort
from app.modules.risk.application.risk_service import validate_key
from app.modules.risk.domain.assessments import RiskReviewCommand, RiskReviewEvent


class RiskReviewService:
    def __init__(self, transactions: RiskTransactionPort):
        self.transactions = transactions

    async def record(
        self, actor: AuthenticatedActor, risk_id: UUID, command: RiskReviewCommand, key: str
    ) -> RiskReviewEvent:
        validate_key(key)
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            return await repo.review(risk_id, command, key)

    async def list(self, actor: AuthenticatedActor, risk_id: UUID) -> tuple[RiskReviewEvent, ...]:
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            return await repo.reviews(risk_id)
