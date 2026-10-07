"""Bounded tenant cleanup; all source scrubs and count evidence commit atomically."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from uuid import UUID

from ..domain.retention import PurgeResult, RetentionPayloadPort


class RetentionService:
    def __init__(
        self, transactions: Callable[[UUID], AbstractAsyncContextManager[RetentionPayloadPort]]
    ):
        self.transactions = transactions

    async def purge_once(
        self, *, organization_id: UUID, now: datetime, limit: int = 100
    ) -> PurgeResult:
        if now.tzinfo is None or not 1 <= limit <= 100:
            raise ValueError("INVALID_RETENTION_BATCH")
        async with self.transactions(organization_id) as repo:
            locators = await repo.claim_expired(organization_id, now, limit)
            for locator in locators:
                await repo.purge(locator, locator.fence)
            if locators:
                await repo.evidence(len(locators), now)
            return PurgeResult(purged=len(locators))
