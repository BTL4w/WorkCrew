"""Fixed-host Exa Auto/Contents adapter; no SDK retries, synthesis or live reader."""

import asyncio
import hashlib
import json
import math
import socket
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app.modules.retrieval.domain.models import (
    PageReadResult,
    ProviderUsage,
    PublicPageRequest,
    PublicSearchRequest,
    RetrievalProviderError,
    RetrievedSource,
    SearchBatch,
)
from app.modules.retrieval.domain.public_urls import ValidatedPublicURL, validate_public_url

_ENDPOINT = "https://api.exa.ai"
_MAX_RESPONSE_BYTES = 1048576
_MAX_TEXT_BYTES = 65536
_VERSION = "exa.auto-cache-only.v1"


class _ProviderModel(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)


class _Result(_ProviderModel):
    url: str
    title: str | None = None
    text: str | None = None
    highlights: tuple[str, ...] = Field(default=(), max_length=20)
    publishedDate: str | None = None


class _Status(_ProviderModel):
    id: str
    status: str
    source: str | None = None


class _Notice(_ProviderModel):
    code: str


class _Cost(_ProviderModel):
    total: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)


class _Response(_ProviderModel):
    results: tuple[_Result, ...] = Field(max_length=5)
    statuses: tuple[_Status, ...] = Field(default=(), max_length=5)
    warnings: tuple[_Notice, ...] = Field(default=(), max_length=20)
    notices: tuple[_Notice, ...] = Field(default=(), max_length=20)
    costDollars: _Cost | None = None


def _normalize(value: str) -> tuple[str, bool]:
    normalized = unicodedata.normalize(
        "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
    ).strip()
    encoded = normalized.encode("utf-8")
    return encoded[:_MAX_TEXT_BYTES].decode("utf-8", errors="ignore"), len(
        encoded
    ) > _MAX_TEXT_BYTES


def _published(value: str | None) -> datetime | None:
    if value is not None:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed
    return None


def _usage(response: _Response) -> ProviderUsage:
    return ProviderUsage(
        requests=1, cost_usd=response.costDollars.total if response.costDollars else None
    )


def _source(
    result: _Result,
    url: ValidatedPublicURL,
    text: str,
    method: Literal["SEARCH_SNIPPET", "PAGE_CONTENT"],
    observed_at: datetime,
) -> RetrievedSource:
    title, _ = _normalize(result.title or url.hostname)
    return RetrievedSource(
        url=url,
        title=title[:500] or url.hostname,
        text=text,
        method=method,
        fingerprint=hashlib.sha256(text.encode()).hexdigest(),
        published_at=_published(result.publishedDate),
        source_fetched_at=None,
        observed_at=observed_at,
        provider_version=_VERSION,
    )


class SystemPublicDNSResolver:
    """Address lookup only, under the service deadline. Never opens an origin socket."""

    async def resolve(self, hostname: str) -> tuple[str, ...]:
        try:
            answers = await asyncio.get_running_loop().getaddrinfo(
                hostname, None, type=socket.SOCK_STREAM
            )
        except OSError:
            raise RetrievalProviderError("PUBLIC_DNS_UNAVAILABLE", provider_requests=0) from None
        return tuple(sorted({str(answer[4][0]) for answer in answers}))


class ExaAdapter:
    def __init__(
        self, api_key: SecretStr, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        key = api_key.get_secret_value()
        if not key or len(key) > 512 or any(not 33 <= ord(char) <= 126 for char in key):
            raise ValueError("EXA_API_KEY_REQUIRED")
        self._api_key = api_key
        self._transport = transport

    async def _request(
        self,
        path: Literal["/search", "/contents"],
        payload: dict[str, object],
        timeout_seconds: float,
    ) -> _Response:
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 8:
            raise ValueError("INVALID_RETRIEVAL_TIMEOUT")
        try:
            async with (
                asyncio.timeout(timeout_seconds),
                httpx.AsyncClient(
                    base_url=_ENDPOINT,
                    timeout=timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport,
                    headers={
                        "x-api-key": self._api_key.get_secret_value(),
                        "Accept-Encoding": "identity",
                    },
                ) as client,
                client.stream("POST", path, json=payload) as response,
            ):
                status = response.status_code
                if 300 <= status < 400:
                    raise RetrievalProviderError("PROVIDER_REDIRECT_BLOCKED")
                if status != 200:
                    code = {
                        401: "PROVIDER_AUTH_FAILED",
                        403: "PROVIDER_AUTH_FAILED",
                        402: "PROVIDER_CREDITS_EXHAUSTED",
                        429: "PROVIDER_RATE_LIMITED",
                    }.get(
                        status,
                        "PROVIDER_UNAVAILABLE" if status >= 500 else "PROVIDER_REQUEST_REJECTED",
                    )
                    raise RetrievalProviderError(code, retryable=status == 429 or status >= 500)
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise RetrievalProviderError("PROVIDER_UNSUPPORTED_ENCODING")
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise RetrievalProviderError("PROVIDER_RESPONSE_TOO_LARGE")
                    body.extend(chunk)
                return _Response.model_validate(json.loads(body))
        except (httpx.TimeoutException, TimeoutError):
            raise RetrievalProviderError("PROVIDER_TIMEOUT", retryable=True) from None
        except httpx.HTTPError:
            raise RetrievalProviderError("PROVIDER_UNAVAILABLE", retryable=True) from None
        except (ValidationError, ValueError, UnicodeError, RecursionError):
            raise RetrievalProviderError("PROVIDER_INVALID_RESPONSE") from None

    async def search(self, request: PublicSearchRequest, *, timeout_seconds: float) -> SearchBatch:
        request = PublicSearchRequest(request.query, request.validated_filters, request.limit)
        payload: dict[str, object] = {
            "query": request.query,
            "type": "auto",
            "numResults": request.limit,
            "contents": {"highlights": True},
        }
        filters = request.validated_filters
        if filters.include_domains:
            payload["includeDomains"] = list(filters.include_domains)
        if filters.exclude_domains:
            payload["excludeDomains"] = list(filters.exclude_domains)
        if filters.published_after is not None:
            payload["startPublishedDate"] = filters.published_after.isoformat()
        if filters.published_before is not None:
            payload["endPublishedDate"] = filters.published_before.isoformat()
        raw = await self._request("/search", payload, timeout_seconds)
        observed_at = datetime.now(UTC)
        warnings: list[str] = []
        filters_verified = not (filters.is_active and (raw.warnings or raw.notices))
        if raw.warnings or raw.notices:
            warnings.append(
                "PROVIDER_FILTERS_UNVERIFIED" if filters.is_active else "PROVIDER_NOTICE"
            )
        sources: dict[str, RetrievedSource] = {}
        for result in raw.results[: request.limit]:
            try:
                url = validate_public_url(result.url)
            except ValueError:
                warnings.append("INVALID_SOURCE_URL")
                continue
            if not filters.accepts(url, _published(result.publishedDate)):
                warnings.append("SOURCE_FILTER_MISMATCH")
                continue
            # No fallback to provider text/summary: highlights are explicitly snippets.
            try:
                text, truncated = _normalize("\n".join(result.highlights))
                source = _source(result, url, text, "SEARCH_SNIPPET", observed_at)
            except UnicodeError:
                warnings.append("INVALID_SOURCE_CONTENT")
                continue
            if not text:
                warnings.append("EMPTY_SOURCE_CONTENT")
                continue
            if truncated:
                warnings.append("SOURCE_TRUNCATED")
            sources.setdefault(url.value, source)
        unavailable = not filters_verified or any(
            warning != "SOURCE_FILTER_MISMATCH" for warning in warnings
        )
        return SearchBatch(
            sources=tuple(sources.values()),
            warnings=tuple(dict.fromkeys(warnings)),
            observed_at=observed_at,
            provider_usage=_usage(raw),
            filters_verified=bool(filters_verified),
            safe_code=None if sources else ("SOURCE_UNAVAILABLE" if unavailable else "NOT_FOUND"),
        )

    async def read(self, request: PublicPageRequest, *, timeout_seconds: float) -> PageReadResult:
        request = PublicPageRequest(request.url)
        raw = await self._request(
            "/contents",
            {
                "urls": [request.url.value],
                "text": True,
                "maxAgeHours": -1,
                "subpages": 0,
            },
            timeout_seconds,
        )
        usage = _usage(raw)
        for status in raw.statuses:
            try:
                matches = validate_public_url(status.id) == request.url
            except ValueError:
                matches = False
            if not matches:
                return PageReadResult(None, "SOURCE_URL_MISMATCH", usage)
            if status.source is not None and status.source != "cached":
                return PageReadResult(None, "CACHE_POLICY_VIOLATION", usage)
            if status.status != "success":
                return PageReadResult(None, "CACHE_UNAVAILABLE", usage)
        if not raw.results:
            return PageReadResult(None, "CACHE_UNAVAILABLE", usage)
        if len(raw.results) != 1:
            return PageReadResult(None, "SOURCE_URL_MISMATCH", usage)
        result = raw.results[0]
        try:
            url = validate_public_url(result.url)
        except ValueError:
            return PageReadResult(None, "SOURCE_URL_MISMATCH", usage)
        if url != request.url:
            return PageReadResult(None, "SOURCE_URL_MISMATCH", usage)
        try:
            text, truncated = _normalize(result.text or "")
            source = _source(result, url, text, "PAGE_CONTENT", datetime.now(UTC))
        except UnicodeError:
            return PageReadResult(None, "INVALID_SOURCE_CONTENT", usage)
        if not text:
            return PageReadResult(None, "EMPTY_SOURCE_CONTENT", usage)
        warnings = ["SOURCE_FRESHNESS_UNKNOWN"]
        if raw.warnings or raw.notices:
            warnings.append("PROVIDER_NOTICE")
        if truncated:
            warnings.append("SOURCE_TRUNCATED")
        return PageReadResult(
            source,
            None,
            usage,
            tuple(warnings),
        )
