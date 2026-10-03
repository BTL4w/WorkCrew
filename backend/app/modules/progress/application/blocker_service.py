"""Manual lifecycle writes and transaction-scoped report integration port."""

import hashlib
import json
from contextlib import AbstractAsyncContextManager
from typing import Protocol
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.progress.domain.blockers import (
    Blocker,
    BlockerCommand,
    BlockerError,
    BlockerTransition,
)


class BlockerRepository(Protocol):
    async def authenticate(self) -> None: ...
    async def authorize(self, task_id: UUID) -> None: ...
    async def apply(self, command: BlockerCommand, update_id: UUID | None = None) -> Blocker: ...
    async def list(self, task_id: UUID) -> tuple[Blocker, ...]: ...
    async def lifecycle_history(self, blocker_id: UUID) -> tuple[BlockerTransition, ...]: ...
    async def replay(
        self, operation: str, key: str, fingerprint: str
    ) -> dict[str, object] | None: ...
    async def remember(
        self, operation: str, key: str, fingerprint: str, result: dict[str, object]
    ) -> None: ...
    async def audit(
        self,
        action: str,
        request_id: str,
        key: str | None,
        resource_id: UUID | None,
        code: str | None = None,
    ) -> None: ...


class BlockerTransactions(Protocol):
    def __call__(
        self, actor: AuthenticatedActor
    ) -> AbstractAsyncContextManager[BlockerRepository]: ...


class BlockerService:
    def __init__(self, transactions: BlockerTransactions):
        self.transactions = transactions

    async def apply(
        self,
        actor: AuthenticatedActor,
        command: BlockerCommand,
        idempotency_key: str,
        request_id: str = "blocker",
    ) -> Blocker:
        fingerprint = hashlib.sha256(
            json.dumps(command.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        try:
            if not 16 <= len(idempotency_key) <= 128:
                raise BlockerError("IDEMPOTENCY_KEY_REQUIRED", 400)
            async with self.transactions(actor) as repo:
                await repo.authenticate()
                await repo.authorize(command.task_id)
                replay = await repo.replay("blocker.apply", idempotency_key, fingerprint)
                if replay is not None:
                    return Blocker.model_validate(replay)
                result = await repo.apply(command)
                await repo.remember(
                    "blocker.apply", idempotency_key, fingerprint, result.model_dump(mode="json")
                )
                await repo.audit(
                    "blocker." + command.action.lower(), request_id, idempotency_key, result.id
                )
                return result
        except BlockerError as exc:
            await self.reject(actor, request_id, idempotency_key, exc.code, command.blocker_id)
            raise

    async def list(self, actor: AuthenticatedActor, task_id: UUID) -> tuple[Blocker, ...]:
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            return await repo.list(task_id)

    async def history(
        self, actor: AuthenticatedActor, blocker_id: UUID
    ) -> tuple[BlockerTransition, ...]:
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            return await repo.lifecycle_history(blocker_id)

    async def reject(
        self,
        actor: AuthenticatedActor,
        request_id: str,
        key: str | None,
        code: str,
        resource_id: UUID | None = None,
    ) -> None:
        async with self.transactions(actor) as repo:
            await repo.audit(
                "blocker.rejected",
                request_id,
                key if key and len(key) <= 128 else None,
                resource_id,
                code,
            )

    async def audit_transport_rejection(
        self, *, actor: AuthenticatedActor, request_id: str, key: str | None, reason_code: str
    ) -> None:
        await self.reject(actor, request_id, key, reason_code)
