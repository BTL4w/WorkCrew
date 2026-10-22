"""Synthetic local adapter behind the same ports; never reads keys or uses network."""

import hashlib
from datetime import UTC, datetime

from app.modules.retrieval.domain.models import (
    PageReadResult,
    ProviderUsage,
    PublicPageRequest,
    PublicSearchRequest,
    RetrievedSource,
    SearchBatch,
)
from app.modules.retrieval.domain.public_urls import validate_public_url


class MockRetrievalAdapter:
    async def search(self, request: PublicSearchRequest, *, timeout_seconds: float) -> SearchBatch:
        request = PublicSearchRequest(request.query, request.validated_filters, request.limit)
        now = datetime.now(UTC)
        url = validate_public_url("https://example.org/guide")
        text = "Public planning guide: review the goal and constraints."
        source = RetrievedSource(
            url,
            "Synthetic planning guide",
            text,
            "SEARCH_SNIPPET",
            hashlib.sha256(text.encode()).hexdigest(),
            now,
            None,
            None,
            "mock.web.v1",
        )
        sources = (source,) if request.validated_filters.accepts(url, None) else ()
        return SearchBatch(
            sources, (), now, ProviderUsage(0), safe_code=None if sources else "NOT_FOUND"
        )

    async def read(self, request: PublicPageRequest, *, timeout_seconds: float) -> PageReadResult:
        request = PublicPageRequest(request.url)
        if request.url.value != "https://example.org/guide":
            return PageReadResult(None, "CACHE_UNAVAILABLE", ProviderUsage(0))
        text = "Review the goal and constraints. Keep a record of the public evidence."
        return PageReadResult(
            RetrievedSource(
                request.url,
                "Synthetic planning guide",
                text,
                "PAGE_CONTENT",
                hashlib.sha256(text.encode()).hexdigest(),
                datetime.now(UTC),
                None,
                None,
                "mock.web.v1",
            ),
            None,
            ProviderUsage(0),
            ("SOURCE_FRESHNESS_UNKNOWN",),
        )
