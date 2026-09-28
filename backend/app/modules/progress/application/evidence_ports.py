"""Application-owned storage and transaction ports."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.progress.domain.evidence import (
    EvidenceOriginal,
    EvidenceVersionRef,
    StoredBlob,
    UploadReservation,
)


class EvidenceStorage(Protocol):
    async def put(self, key: str, source: AsyncIterator[bytes], max_bytes: int) -> StoredBlob: ...
    def open(self, key: str) -> AsyncIterator[bytes]: ...
    async def delete(self, key: str) -> None: ...


class EvidenceRepository(Protocol):
    async def reserve(self, filename: str, idempotency_key: str) -> UploadReservation: ...
    async def finalize(self, reservation: UploadReservation, blob: StoredBlob) -> bool: ...
    async def get(self, ref: EvidenceVersionRef) -> EvidenceOriginal: ...
    async def release(self, reservation: UploadReservation) -> None: ...
    async def audit(
        self,
        action: str,
        request_id: str,
        code: str | None = None,
        resource_id: UUID | None = None,
        idempotency_key: str | None = None,
    ) -> None: ...
    async def expired(self, at: datetime) -> tuple[UploadReservation, ...]: ...
    async def expire(self, reservation: UploadReservation) -> None: ...


EvidenceTransactionFactory = Callable[
    [AuthenticatedActor], AbstractAsyncContextManager[EvidenceRepository]
]
