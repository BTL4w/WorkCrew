"""Current recipient authorization precedes delivery or replay."""

from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.risk.application.ports import RiskTransactionPort
from app.modules.risk.application.risk_service import validate_key
from app.modules.risk.domain.notifications import RiskNotification


class NotificationService:
    def __init__(self, transactions: RiskTransactionPort):
        self.transactions = transactions

    async def deliver(self, actor: AuthenticatedActor, risk_id: UUID) -> None:
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            await repo.notify(risk_id)

    async def list(self, actor: AuthenticatedActor) -> tuple[RiskNotification, ...]:
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            return await repo.notifications()

    async def mark_read(self, actor: AuthenticatedActor, id: UUID, key: str) -> RiskNotification:
        validate_key(key)
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            return await repo.read_notification(id, key)
