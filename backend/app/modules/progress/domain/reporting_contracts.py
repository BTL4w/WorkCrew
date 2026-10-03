"""Shared immutable reporting values without lifecycle dependencies."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ReportingContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SelectedEvidence(ReportingContract):
    evidence_id: UUID
    version: int = Field(ge=1)

    def __hash__(self) -> int:
        return hash((self.evidence_id, self.version))
