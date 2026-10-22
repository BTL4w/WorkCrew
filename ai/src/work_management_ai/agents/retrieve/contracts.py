"""Bounded web evidence and synthesis schemas, independent of Exa and persistence."""

import ipaddress
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


class RetrievalContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _public_http_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.port not in {None, 80, 443}
        or parsed.hostname.lower() == "localhost"
        or parsed.hostname.lower().endswith((".localhost", ".local", ".internal"))
    ):
        raise ValueError("public HTTP URL required")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass  # DNS, redirect and query-string egress validation belong to the application service.
    else:
        if not address.is_global:
            raise ValueError("public IP required")
    return value


type PublicURL = Annotated[
    str, StringConstraints(min_length=1, max_length=2048), AfterValidator(_public_http_url)
]
type Fingerprint = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
type ShortText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
]
type SafeCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]


class RetrieveLimits(RetrievalContract):
    max_queries: int = Field(default=2, ge=0, le=2, strict=True)
    results_per_query: int = Field(default=5, ge=0, le=5, strict=True)
    max_page_reads: int = Field(default=3, ge=0, le=3, strict=True)
    max_tool_calls: int = Field(default=8, ge=0, le=8, strict=True)
    max_provider_requests: int = Field(default=8, ge=0, le=8, strict=True)
    max_sources: int = Field(default=8, ge=0, le=8, strict=True)
    max_evidence_tokens: int = Field(default=4000, ge=0, le=4000, strict=True)
    max_model_attempts: int = Field(default=2, ge=0, le=2, strict=True)
    max_input_tokens: int = Field(default=12000, ge=0, le=12000, strict=True)
    max_output_tokens: int = Field(default=3200, ge=0, le=3200, strict=True)
    max_output_tokens_per_attempt: int = Field(default=1600, ge=0, le=1600, strict=True)
    retrieval_timeout_seconds: int = Field(default=8, ge=0, le=8, strict=True)
    synthesis_timeout_seconds: int = Field(default=20, ge=0, le=20, strict=True)
    timeout_seconds: int = Field(default=25, ge=0, le=25, strict=True)
    max_iterations: int = Field(default=12, ge=0, le=12, strict=True)
    max_handoffs: Literal[0] = 0
    max_replans: Literal[0] = 0


class SourceFilters(RetrievalContract):
    include_domains: tuple[ShortText, ...] = Field(default=(), max_length=20)
    exclude_domains: tuple[ShortText, ...] = Field(default=(), max_length=20)
    published_after: AwareDatetime | None = None
    published_before: AwareDatetime | None = None

    @model_validator(mode="after")
    def ordered_dates(self) -> Self:
        if (
            self.published_after is not None
            and self.published_before is not None
            and self.published_after > self.published_before
        ):
            raise ValueError("publication date filters reversed")
        return self


class RetrieveRequirement(RetrievalContract):
    requirement_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    description: ShortText
    required: bool = True


class SourceBinding(RetrievalContract):
    evidence_id: UUID
    fingerprint: Fingerprint


class RetrieveInput(RetrievalContract):
    schema_version: Literal["2.0"] = "2.0"
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    locale: Literal["vi", "en"]
    source_mode: Literal["WEB", "DOCUMENTS", "BOTH", "AUTO"] = "WEB"
    answer_mode: Literal["SUMMARY", "EVIDENCE_ONLY"] = "SUMMARY"
    search_queries: tuple[ShortText, ...] = Field(default=(), max_length=2)
    requirements: tuple[RetrieveRequirement, ...] = Field(min_length=1, max_length=8)
    filters: SourceFilters = Field(default_factory=SourceFilters)
    limits: RetrieveLimits = Field(default_factory=RetrieveLimits)
    explicit_public_urls: tuple[PublicURL, ...] = Field(default=(), max_length=3)
    document_refs: tuple[UUID, ...] = Field(default=(), max_length=8)
    collection_ref: UUID | None = None

    @model_validator(mode="after")
    def bounded_selectors(self) -> Self:
        ids = [item.requirement_id for item in self.requirements]
        if len(ids) != len(set(ids)):
            raise ValueError("requirement IDs must be unique")
        if len(self.search_queries) > self.limits.max_queries:
            raise ValueError("queries exceed requested limit")
        if len(self.explicit_public_urls) > self.limits.max_page_reads:
            raise ValueError("URLs exceed requested page limit")
        return self


class WebEvidence(RetrievalContract):
    evidence_id: UUID
    canonical_url: PublicURL
    title: ShortText
    excerpt: str = Field(min_length=1, max_length=65536)
    method: Literal["SEARCH_SNIPPET", "PAGE_CONTENT"]
    fingerprint: Fingerprint
    published_at: AwareDatetime | None = None
    source_fetched_at: AwareDatetime | None = None
    observed_at: AwareDatetime
    expires_at: AwareDatetime
    provider_version: str = Field(min_length=1, max_length=100)

    @field_validator("excerpt")
    @classmethod
    def bound_utf8_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 65536:
            raise ValueError("source text exceeds 64 KiB")
        return value

    @model_validator(mode="after")
    def expiry_follows_observation(self) -> Self:
        if self.expires_at <= self.observed_at:
            raise ValueError("source expiry must follow observation")
        return self


class QuoteLocator(SourceBinding):
    text: str = Field(min_length=1, max_length=2000)
    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def quote_span_matches_length(self) -> Self:
        if self.end - self.start != len(self.text):
            raise ValueError("quote span does not match text")
        return self


class GroundedClaim(RetrievalContract):
    text: str = Field(min_length=1, max_length=3000)
    evidence_ids: tuple[UUID, ...] = Field(min_length=1, max_length=8)
    quote: QuoteLocator | None = None


class GroundedSummary(RetrievalContract):
    claims: tuple[GroundedClaim, ...] = Field(default=(), max_length=12)
    limitations: tuple[ShortText, ...] = Field(default=(), max_length=16)
    insufficient_evidence: bool = False


class RetrieveUsage(RetrievalContract):
    model_attempts: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    provider_requests: int = Field(default=0, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    provider_cost_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    model_cost_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)


class SourceCoverage(RetrievalContract):
    source: Literal["WEB", "DOCUMENTS"]
    state: Literal["COMPLETE", "PARTIAL", "NOT_FOUND", "UNAVAILABLE", "NOT_READY"]
    required: bool = True


class RetrieveOutcome(StrEnum):
    ANSWER_READY = "ANSWER_READY"
    EVIDENCE_READY = "EVIDENCE_READY"
    PARTIAL = "PARTIAL"
    NOT_FOUND = "NOT_FOUND"
    NEEDS_INPUT = "NEEDS_INPUT"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    DOCUMENTS_NOT_READY = "DOCUMENTS_NOT_READY"
    REJECTED = "REJECTED"


class RetrieveOutput(RetrievalContract):
    schema_version: Literal["2.0"] = "2.0"
    outcome: RetrieveOutcome
    strategy_used: Literal["WEB", "NONE"]
    summary: GroundedSummary | None = None
    evidence: tuple[WebEvidence, ...] = Field(default=(), max_length=8)
    coverage: tuple[SourceCoverage, ...] = Field(default=(), max_length=2)
    limitations: tuple[ShortText, ...] = Field(default=(), max_length=16)
    question: ShortText | None = None
    observed_at: AwareDatetime
    usage: RetrieveUsage = Field(default_factory=RetrieveUsage)
    safe_error_code: SafeCode | None = None

    @model_validator(mode="after")
    def evidence_bindings_and_outcomes(self) -> Self:
        sources = {item.evidence_id: item for item in self.evidence}
        if len(sources) != len(self.evidence):
            raise ValueError("duplicate evidence ID")
        claims = self.summary.claims if self.summary else ()
        for claim in claims:
            if not set(claim.evidence_ids).issubset(sources):
                raise ValueError("claim references unknown evidence")
            if claim.quote is not None:
                quote = claim.quote
                source = sources.get(quote.evidence_id)
                if (
                    quote.evidence_id not in claim.evidence_ids
                    or source is None
                    or source.method != "PAGE_CONTENT"
                    or source.fingerprint != quote.fingerprint
                ):
                    raise ValueError("quote requires bound page-content evidence")
        if self.outcome is RetrieveOutcome.ANSWER_READY and (
            not claims or self.summary is None or self.summary.insufficient_evidence
        ):
            raise ValueError("answer requires sufficient cited claims")
        if self.outcome is RetrieveOutcome.ANSWER_READY and any(
            item.required and item.state != "COMPLETE" for item in self.coverage
        ):
            raise ValueError("required source coverage is incomplete")
        if self.outcome is RetrieveOutcome.EVIDENCE_READY and (not sources or claims):
            raise ValueError("evidence-only outcome requires sources without claims")
        if self.outcome not in {RetrieveOutcome.ANSWER_READY, RetrieveOutcome.PARTIAL} and claims:
            raise ValueError("non-answer outcome cannot carry claims")
        if (self.outcome is RetrieveOutcome.NEEDS_INPUT) != (self.question is not None):
            raise ValueError("needs-input outcome and question must agree")
        if len({item.source for item in self.coverage}) != len(self.coverage):
            raise ValueError("duplicate source coverage")
        return self
