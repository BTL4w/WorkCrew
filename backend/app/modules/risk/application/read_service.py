"""Current-authority reads and exact source fingerprint verification."""

from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.progress.domain.blockers import BlockerError
from app.modules.risk.application.ports import RiskTransactionPort
from app.modules.risk.domain.read_context import RiskReadContext


class RiskReadService:
    def __init__(self, transactions: RiskTransactionPort):
        self.transactions = transactions

    async def read(
        self, actor: AuthenticatedActor, task_id: UUID, expected_fingerprint: str | None = None
    ) -> RiskReadContext:
        async with self.transactions(actor) as repo:
            value = await repo.read_context(task_id)
            if expected_fingerprint is not None and value.fingerprint != expected_fingerprint:
                raise BlockerError("RISK_CONTEXT_CHANGED", 409)
            return value
