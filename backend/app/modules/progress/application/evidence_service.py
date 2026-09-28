"""Application-owned two-phase evidence persistence and recovery."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePath

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.progress.application.evidence_ports import (
    EvidenceStorage,
    EvidenceTransactionFactory,
)
from app.modules.progress.domain.evidence import (
    MAX_EVIDENCE_BYTES,
    MIME_EXTENSIONS,
    AuthorizedEvidenceStream,
    EvidenceError,
    EvidenceOriginal,
    EvidenceVersionRef,
)


@dataclass(frozen=True, slots=True)
class UploadEvidenceCommand:
    idempotency_key: str
    filename: str
    stream: AsyncIterator[bytes]
    mime_type: str
    request_id: str


class EvidenceService:
    def __init__(self, transactions: EvidenceTransactionFactory, storage: EvidenceStorage) -> None:
        self.transactions = transactions
        self.storage = storage

    async def reject(
        self, actor: AuthenticatedActor, request_id: str, code: str, key: str | None = None
    ) -> None:
        async with self.transactions(actor) as repository:
            await repository.audit(
                "evidence.upload.rejected",
                request_id,
                code,
                idempotency_key=key if key and len(key) <= 128 else None,
            )

    async def upload(
        self, actor: AuthenticatedActor, command: UploadEvidenceCommand
    ) -> EvidenceVersionRef:
        reservation = None
        try:
            filename = command.filename
            suffix = PurePath(filename).suffix.lower()
            if (
                not filename
                or len(filename) > 255
                or "/" in filename
                or "\\" in filename
                or any(ord(c) < 32 or ord(c) == 127 for c in filename)
                or suffix not in MIME_EXTENSIONS
            ):
                raise EvidenceError("EVIDENCE_INVALID_FORMAT")
            if not 16 <= len(command.idempotency_key) <= 128:
                raise EvidenceError("INVALID_REQUEST", 400)
            async with self.transactions(actor) as repository:
                reservation = await repository.reserve(filename, command.idempotency_key)
            async with asyncio.timeout(120):
                blob = await self.storage.put(reservation.key, command.stream, MAX_EVIDENCE_BYTES)
            if (
                blob.detected_mime != MIME_EXTENSIONS[suffix]
                or blob.detected_mime != command.mime_type
            ):
                raise EvidenceError("EVIDENCE_INVALID_FORMAT")
            async with self.transactions(actor) as repository:
                replayed = await repository.finalize(reservation, blob)
                await repository.audit(
                    "evidence.upload.replayed" if replayed else "evidence.uploaded",
                    command.request_id,
                    resource_id=reservation.ref.evidence_id,
                    idempotency_key=command.idempotency_key,
                )
            return reservation.ref
        except EvidenceError as exc:
            if reservation is not None:
                async with self.transactions(actor) as repository:
                    await repository.release(reservation)
            await self.reject(actor, command.request_id, exc.code, command.idempotency_key)
            raise
        except (OSError, TimeoutError) as exc:
            # A published original remains associated with its durable reservation.
            # Retry revalidates bytes and reconciles metadata; no unsafe deletion.
            await self.reject(
                actor, command.request_id, "EVIDENCE_STORAGE_UNAVAILABLE", command.idempotency_key
            )
            raise EvidenceError("EVIDENCE_STORAGE_UNAVAILABLE", 503) from exc

    async def get(self, actor: AuthenticatedActor, ref: EvidenceVersionRef) -> EvidenceOriginal:
        async with self.transactions(actor) as repository:
            return await repository.get(ref)

    async def open_version(
        self,
        actor: AuthenticatedActor,
        ref: EvidenceVersionRef,
        request_id: str = "evidence-download",
    ) -> AuthorizedEvidenceStream:
        stream: AsyncIterator[bytes] | None = None

        async def close() -> None:
            closer = getattr(stream, "aclose", None)
            if closer is not None:
                await closer()

        async def rejected(code: str) -> None:
            await close()
            async with self.transactions(actor) as repository:
                await repository.audit(
                    "evidence.download.rejected", request_id, code, ref.evidence_id
                )

        try:
            original = await self.get(actor, ref)
            stream = self.storage.open(original.blob.key)
            # Prepare before returning headers, then recheck current authorization.
            first = await anext(stream)
            async with self.transactions(actor) as repository:
                await repository.get(ref)
                await repository.audit(
                    "evidence.downloaded", request_id, resource_id=ref.evidence_id
                )
        except (OSError, StopAsyncIteration) as exc:
            await rejected("EVIDENCE_STORAGE_UNAVAILABLE")
            raise EvidenceError("EVIDENCE_STORAGE_UNAVAILABLE", 503) from exc
        except EvidenceError as exc:
            await rejected(exc.code)
            raise
        except BaseException:
            await close()
            raise

        async def content() -> AsyncIterator[bytes]:
            try:
                yield first
                assert stream is not None
                async for part in stream:
                    yield part
            finally:
                await close()

        return AuthorizedEvidenceStream(original, content())

    async def cleanup_expired(self, actor: AuthenticatedActor, at: datetime | None = None) -> int:
        """Safe retryable maintenance: lock metadata across physical deletion."""
        count = 0
        async with self.transactions(actor) as repository:
            for reservation in await repository.expired(at or datetime.now(UTC)):
                await self.storage.delete(reservation.key)
                await repository.expire(reservation)
                await repository.audit(
                    "evidence.staging.expired",
                    "evidence-retention",
                    resource_id=reservation.ref.evidence_id,
                )
                count += 1
        return count
