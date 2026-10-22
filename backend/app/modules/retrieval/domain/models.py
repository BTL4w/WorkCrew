"""Application-owned values; no AI contracts, framework, database or provider SDK."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from app.modules.retrieval.domain.public_urls import (
    ValidatedPublicURL,
    validate_public_query,
    validate_public_url,
)


class RetrievalPolicyError(ValueError):
    """Safe deterministic refusal; never carries raw user/provider content."""


class RetrievalProviderError(Exception):
    def __init__(self, safe_code: str, *, retryable: bool = False, provider_requests: int = 1):
        super().__init__(safe_code)
        self.safe_code = safe_code
        self.retryable = retryable
        self.provider_requests = provider_requests


@dataclass(frozen=True, slots=True)
class ValidatedSourceFilters:
    include_domains: tuple[str, ...] = ()
    exclude_domains: tuple[str, ...] = ()
    published_after: datetime | None = None
    published_before: datetime | None = None

    def __post_init__(self) -> None:
        for name, domains in (
            ("include_domains", self.include_domains),
            ("exclude_domains", self.exclude_domains),
        ):
            if type(domains) is not tuple or len(domains) > 20:
                raise ValueError("INVALID_SOURCE_FILTER")
            normalized: list[str] = []
            for domain in domains:
                if type(domain) is not str or any(char in domain for char in "/:@?#"):
                    raise ValueError("INVALID_SOURCE_FILTER")
                normalized.append(validate_public_url(f"https://{domain}/").hostname)
            object.__setattr__(self, name, tuple(dict.fromkeys(normalized)))
        for date in (self.published_after, self.published_before):
            if date is not None and (date.tzinfo is None or date.utcoffset() is None):
                raise ValueError("INVALID_SOURCE_FILTER")
        if (
            self.published_after
            and self.published_before
            and self.published_after > self.published_before
        ):
            raise ValueError("INVALID_SOURCE_FILTER")

    @property
    def is_active(self) -> bool:
        return bool(
            self.include_domains
            or self.exclude_domains
            or self.published_after
            or self.published_before
        )

    def accepts(self, url: ValidatedPublicURL, published_at: datetime | None) -> bool:
        def matches(domain: str) -> bool:
            return url.hostname == domain or url.hostname.endswith("." + domain)

        return (
            (not self.include_domains or any(matches(domain) for domain in self.include_domains))
            and not any(matches(domain) for domain in self.exclude_domains)
            and (
                self.published_after is None
                or (published_at is not None and published_at >= self.published_after)
            )
            and (
                self.published_before is None
                or (published_at is not None and published_at <= self.published_before)
            )
        )


@dataclass(frozen=True, slots=True)
class PublicSearchRequest:
    query: str
    validated_filters: ValidatedSourceFilters = field(default_factory=ValidatedSourceFilters)
    limit: int = 5

    def __post_init__(self) -> None:
        object.__setattr__(self, "query", validate_public_query(self.query))
        if type(self.limit) is not int or not 1 <= self.limit <= 5:
            raise ValueError("INVALID_SEARCH_LIMIT")
        if type(self.validated_filters) is not ValidatedSourceFilters:
            raise ValueError("INVALID_SOURCE_FILTER")


@dataclass(frozen=True, slots=True)
class PublicPageRequest:
    url: ValidatedPublicURL

    def __post_init__(self) -> None:
        if (
            type(self.url) is not ValidatedPublicURL
            or validate_public_url(self.url.value) != self.url
        ):
            raise ValueError("UNSAFE_PUBLIC_URL")


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Provider-reported estimated cost, never a billing entitlement; absent is unknown."""

    requests: int
    cost_usd: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RetrievedSource:
    url: ValidatedPublicURL
    title: str
    text: str
    method: Literal["SEARCH_SNIPPET", "PAGE_CONTENT"]
    fingerprint: str
    observed_at: datetime
    published_at: datetime | None
    source_fetched_at: datetime | None
    provider_version: str


@dataclass(frozen=True, slots=True)
class SearchBatch:
    sources: tuple[RetrievedSource, ...]
    warnings: tuple[str, ...]
    observed_at: datetime
    provider_usage: ProviderUsage
    filters_verified: bool = True
    safe_code: str | None = None


@dataclass(frozen=True, slots=True)
class PageReadResult:
    source: RetrievedSource | None
    safe_code: str | None
    provider_usage: ProviderUsage
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetrievalReservation:
    """Receipt identity, not authority: required verifier rechecks durable state."""

    id: UUID
    organization_id: UUID
    membership_id: UUID
    agent_run_id: UUID
    operation_key: str
    attempt_no: int
    fencing_token: UUID
    deadline: datetime
    kind: str

    def __post_init__(self) -> None:
        if (
            self.kind not in {"SEARCH", "PAGE_READ", "MODEL", "NODE", "TOOL"}
            or not 1 <= len(self.operation_key) <= 200
            or type(self.attempt_no) is not int
            or not 1 <= self.attempt_no <= 2
            or self.deadline.tzinfo is None
            or self.deadline.utcoffset() is None
        ):
            raise RetrievalPolicyError("INVALID_RETRIEVAL_RESERVATION")
