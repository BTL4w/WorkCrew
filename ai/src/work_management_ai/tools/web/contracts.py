"""Provider-neutral read-only tool schemas. No role or tenant comes from model input."""

from datetime import datetime
from uuid import UUID

from pydantic import Field

from work_management_ai.agents.retrieve.contracts import (
    PublicURL,
    RetrievalContract,
    RetrieveUsage,
    SafeCode,
    ShortText,
    SourceBinding,
    SourceFilters,
    WebEvidence,
)


class WebSearchInput(RetrievalContract):
    query: ShortText
    filters: SourceFilters = Field(default_factory=SourceFilters)
    limit: int = Field(default=5, ge=1, le=5, strict=True)


class WebSearchOutput(RetrievalContract):
    evidence: tuple[WebEvidence, ...] = Field(default=(), max_length=5)
    limitations: tuple[ShortText, ...] = Field(default=(), max_length=8)
    usage: RetrieveUsage = Field(default_factory=RetrieveUsage)


class WebReadPagesInput(RetrievalContract):
    urls: tuple[PublicURL, ...] = Field(min_length=1, max_length=3)


class WebReadPagesOutput(RetrievalContract):
    evidence: tuple[WebEvidence, ...] = Field(default=(), max_length=3)
    limitations: tuple[ShortText, ...] = Field(default=(), max_length=8)
    usage: RetrieveUsage = Field(default_factory=RetrieveUsage)


class WebRevalidateInput(RetrievalContract):
    sources: tuple[SourceBinding, ...] = Field(min_length=1, max_length=8)


class WebRevalidateOutput(RetrievalContract):
    valid_evidence_ids: tuple[UUID, ...] = Field(default=(), max_length=8)
    observed_at: datetime
    safe_codes: tuple[SafeCode, ...] = Field(default=(), max_length=8)
