"""Public original evidence metadata; private storage keys stay server-side."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.progress.domain.evidence import EvidenceOriginal


class EvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: UUID
    version: int = Field(ge=1)
    filename: str
    mime_type: str
    byte_length: int = Field(gt=0, le=20 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    uploaded_at: datetime
    expires_at: datetime | None

    @classmethod
    def from_original(cls, original: EvidenceOriginal) -> "EvidenceResponse":
        return cls(
            evidence_id=original.ref.evidence_id,
            version=original.ref.version,
            filename=original.filename,
            mime_type=original.blob.detected_mime,
            byte_length=original.blob.byte_length,
            sha256=original.blob.sha256,
            uploaded_at=original.uploaded_at,
            expires_at=original.expires_at,
        )
