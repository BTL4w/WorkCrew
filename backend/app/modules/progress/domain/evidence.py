"""Immutable original evidence and safe application errors."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

MAX_EVIDENCE_BYTES = 20 * 1024 * 1024
MIME_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


class EvidenceError(Exception):
    def __init__(self, code: str, status: int = 422) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class EvidenceVersionRef:
    evidence_id: UUID
    version: int


@dataclass(frozen=True, slots=True)
class StoredBlob:
    key: str
    sha256: str
    byte_length: int
    detected_mime: str


@dataclass(frozen=True, slots=True)
class EvidenceOriginal:
    ref: EvidenceVersionRef
    filename: str
    blob: StoredBlob
    uploaded_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class UploadReservation:
    ref: EvidenceVersionRef
    key: str
    lease_id: UUID


@dataclass(frozen=True, slots=True)
class AuthorizedEvidenceStream:
    original: EvidenceOriginal
    stream: AsyncIterator[bytes]
