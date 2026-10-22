"""MockTransport exercises the real Exa HTTP boundary without credentials/network."""

import hashlib
import json
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr


def _result(url: str = "https://example.org/guide") -> dict[str, object]:
    return {"url": url, "title": "Public guide", "highlights": ["Public planning advice."]}


@pytest.mark.asyncio
async def test_exa_auto_has_five_results_and_no_synthesis() -> None:
    from app.modules.retrieval.adapters.exa import ExaAdapter
    from app.modules.retrieval.domain.models import PublicSearchRequest

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"results": [_result()], "costDollars": {"total": 0.005}})

    adapter = ExaAdapter(SecretStr("test-key"), transport=httpx.MockTransport(handler))
    batch = await adapter.search(PublicSearchRequest("public planning"), timeout_seconds=1)
    assert len(captured) == 1
    assert str(captured[0].url) == "https://api.exa.ai/search"
    assert captured[0].headers["x-api-key"] == "test-key"
    assert json.loads(captured[0].content) == {
        "query": "public planning",
        "type": "auto",
        "numResults": 5,
        "contents": {"highlights": True},
    }
    assert batch.sources[0].method == "SEARCH_SNIPPET"
    assert batch.sources[0].published_at is None
    assert batch.sources[0].source_fetched_at is None
    assert batch.provider_usage.requests == 1
    assert batch.provider_usage.cost_usd == Decimal("0.005")


@pytest.mark.asyncio
async def test_no_secret_or_history_in_exa_payload() -> None:
    from app.modules.retrieval.adapters.exa import ExaAdapter
    from app.modules.retrieval.domain.models import PublicSearchRequest

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    adapter = ExaAdapter(SecretStr("provider-test-secret"), transport=httpx.MockTransport(handler))
    await adapter.search(PublicSearchRequest("public planning"), timeout_seconds=1)
    assert set(json.loads(requests[0].content)) == {"query", "type", "numResults", "contents"}
    assert b"provider-test-secret" not in requests[0].content
    assert "provider-test-secret" not in repr(adapter)
    with pytest.raises(TypeError):
        PublicSearchRequest(query="public", history="private")  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_contents_cache_only_never_enables_livecrawl() -> None:
    from app.modules.retrieval.adapters.exa import ExaAdapter
    from app.modules.retrieval.domain.models import PublicPageRequest
    from app.modules.retrieval.domain.public_urls import validate_public_url

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "results": [{**_result(), "text": "  Plan\r\nReview  "}],
                "statuses": [
                    {"id": "https://example.org/guide", "status": "success", "source": "cached"}
                ],
            },
        )

    adapter = ExaAdapter(SecretStr("test-key"), transport=httpx.MockTransport(handler))
    result = await adapter.read(
        PublicPageRequest(validate_public_url("https://example.org/guide")), timeout_seconds=1
    )
    assert len(captured) == 1
    assert str(captured[0].url) == "https://api.exa.ai/contents"
    assert json.loads(captured[0].content) == {
        "urls": ["https://example.org/guide"],
        "text": True,
        "maxAgeHours": -1,
        "subpages": 0,
    }
    assert result.source is not None
    assert result.source.method == "PAGE_CONTENT"
    assert result.source.text == "Plan\nReview"
    assert result.source.fingerprint == hashlib.sha256(b"Plan\nReview").hexdigest()
    assert result.source.source_fetched_at is None


@pytest.mark.asyncio
async def test_partial_contents_retains_good_sources() -> None:
    from app.modules.retrieval.adapters.exa import ExaAdapter
    from app.modules.retrieval.domain.models import PublicPageRequest
    from app.modules.retrieval.domain.public_urls import validate_public_url

    def handler(request: httpx.Request) -> httpx.Response:
        url = json.loads(request.content)["urls"][0]
        if url.endswith("missing"):
            return httpx.Response(
                200,
                json={
                    "results": [],
                    "statuses": [
                        {"id": url, "status": "error", "error": {"tag": "CACHE_MISS"}},
                    ],
                },
            )
        return httpx.Response(200, json={"results": [{**_result(url), "text": "Useful evidence"}]})

    adapter = ExaAdapter(SecretStr("test-key"), transport=httpx.MockTransport(handler))
    good = await adapter.read(
        PublicPageRequest(validate_public_url("https://example.org/guide")), timeout_seconds=1
    )
    missing = await adapter.read(
        PublicPageRequest(validate_public_url("https://example.org/missing")), timeout_seconds=1
    )
    assert good.source is not None and good.source.text == "Useful evidence"
    assert missing.source is None
    assert missing.safe_code == "CACHE_UNAVAILABLE"
    assert missing.provider_usage.requests == 1
    assert missing.provider_usage.cost_usd is None


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (402, "PROVIDER_CREDITS_EXHAUSTED", False),
        (429, "PROVIDER_RATE_LIMITED", True),
        (401, "PROVIDER_AUTH_FAILED", False),
        (503, "PROVIDER_UNAVAILABLE", True),
    ],
)
@pytest.mark.asyncio
async def test_timeout_429_empty_and_402_are_distinct(
    status: int, code: str, retryable: bool
) -> None:
    from app.modules.retrieval.adapters.exa import ExaAdapter
    from app.modules.retrieval.domain.models import PublicSearchRequest, RetrievalProviderError

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, text="provider-test-secret and private raw error")

    adapter = ExaAdapter(SecretStr("provider-test-secret"), transport=httpx.MockTransport(handler))
    with pytest.raises(RetrievalProviderError) as error:
        await adapter.search(PublicSearchRequest("public"), timeout_seconds=1)
    assert calls == 1
    assert error.value.safe_code == code
    assert error.value.retryable == retryable
    assert error.value.provider_requests == 1
    assert "private" not in str(error.value)
    assert "provider-test-secret" not in repr(error.value)


@pytest.mark.asyncio
async def test_timeout_and_empty_search_have_different_outcomes() -> None:
    from app.modules.retrieval.adapters.exa import ExaAdapter
    from app.modules.retrieval.domain.models import PublicSearchRequest, RetrievalProviderError

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private error", request=request)

    adapter = ExaAdapter(SecretStr("test-key"), transport=httpx.MockTransport(timeout))
    with pytest.raises(RetrievalProviderError, match="PROVIDER_TIMEOUT"):
        await adapter.search(PublicSearchRequest("public"), timeout_seconds=1)
    empty = ExaAdapter(
        SecretStr("test-key"),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"results": []})),
    )
    result = await empty.search(PublicSearchRequest("public"), timeout_seconds=1)
    assert result.sources == () and result.safe_code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_redirect_is_not_followed_and_raw_location_is_not_exposed() -> None:
    from app.modules.retrieval.adapters.exa import ExaAdapter
    from app.modules.retrieval.domain.models import PublicSearchRequest, RetrievalProviderError

    calls: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/private"})

    adapter = ExaAdapter(SecretStr("test-key"), transport=httpx.MockTransport(redirect))
    with pytest.raises(RetrievalProviderError, match="PROVIDER_REDIRECT_BLOCKED"):
        await adapter.search(PublicSearchRequest("public"), timeout_seconds=1)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_returned_page_url_mismatch_never_fetches_a_second_url() -> None:
    from app.modules.retrieval.adapters.exa import ExaAdapter
    from app.modules.retrieval.domain.models import PublicPageRequest
    from app.modules.retrieval.domain.public_urls import validate_public_url

    calls: list[httpx.Request] = []

    def mismatch(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json={"results": [{**_result("http://127.0.0.1/"), "text": "secret"}]}
        )

    result = await ExaAdapter(SecretStr("test-key"), transport=httpx.MockTransport(mismatch)).read(
        PublicPageRequest(validate_public_url("https://example.org/guide")), timeout_seconds=1
    )
    assert result.source is None
    assert result.safe_code == "SOURCE_URL_MISMATCH"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_oversized_stream_stops_before_parse() -> None:
    from app.modules.retrieval.adapters.exa import ExaAdapter
    from app.modules.retrieval.domain.models import PublicSearchRequest, RetrievalProviderError

    class Oversized(httpx.AsyncByteStream):
        consumed = 0
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(40):
                self.consumed += 1
                yield b"x" * 65536

        async def aclose(self) -> None:
            self.closed = True

    stream = Oversized()
    adapter = ExaAdapter(
        SecretStr("test-key"),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)),
    )
    with pytest.raises(RetrievalProviderError, match="PROVIDER_RESPONSE_TOO_LARGE"):
        await adapter.search(PublicSearchRequest("public"), timeout_seconds=1)
    assert stream.consumed < 40
    assert stream.closed


@pytest.mark.asyncio
async def test_ignored_filters_and_missing_dates_are_explicit_limitations() -> None:
    from app.modules.retrieval.adapters.exa import ExaAdapter
    from app.modules.retrieval.domain.models import PublicSearchRequest, ValidatedSourceFilters

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "results": [_result(), _result("https://unrelated.org/guide")],
                "warnings": [
                    {"code": "IGNORED_FIELD", "path": "/includeDomains", "message": "raw"}
                ],
            },
        )

    batch = await ExaAdapter(SecretStr("test-key"), transport=httpx.MockTransport(handler)).search(
        PublicSearchRequest(
            "public", validated_filters=ValidatedSourceFilters(include_domains=("example.org",))
        ),
        timeout_seconds=1,
    )
    assert json.loads(captured[0].content)["includeDomains"] == ["example.org"]
    assert len(batch.sources) == 1
    assert not batch.filters_verified
    assert "PROVIDER_FILTERS_UNVERIFIED" in batch.warnings
    assert batch.sources[0].published_at is None


@pytest.mark.asyncio
async def test_normalized_text_cap_counts_utf8_bytes() -> None:
    from app.modules.retrieval.adapters.exa import ExaAdapter
    from app.modules.retrieval.domain.models import PublicPageRequest
    from app.modules.retrieval.domain.public_urls import validate_public_url

    adapter = ExaAdapter(
        SecretStr("test-key"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"results": [{**_result(), "text": "😀" * 20000}]}
            )
        ),
    )
    result = await adapter.read(
        PublicPageRequest(validate_public_url("https://example.org/guide")), timeout_seconds=1
    )
    assert result.source is not None
    assert len(result.source.text.encode()) <= 65536
    assert "SOURCE_TRUNCATED" in result.warnings


@pytest.mark.parametrize("bad_text", ["\ud800", "bad\udffftext"])
@pytest.mark.asyncio
async def test_invalid_unicode_source_is_a_safe_limitation(bad_text: str) -> None:
    from app.modules.retrieval.adapters.exa import ExaAdapter
    from app.modules.retrieval.domain.models import PublicPageRequest
    from app.modules.retrieval.domain.public_urls import validate_public_url

    payload = json.dumps({"results": [{**_result(), "text": bad_text}]})
    adapter = ExaAdapter(
        SecretStr("test-key"),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=payload)),
    )
    result = await adapter.read(
        PublicPageRequest(validate_public_url("https://example.org/guide")), timeout_seconds=1
    )
    assert result.source is None and result.safe_code == "INVALID_SOURCE_CONTENT"


@pytest.mark.asyncio
async def test_invalid_search_sources_are_not_misreported_as_no_matches() -> None:
    from app.modules.retrieval.adapters.exa import ExaAdapter
    from app.modules.retrieval.domain.models import PublicSearchRequest

    adapter = ExaAdapter(
        SecretStr("test-key"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"results": [_result("http://127.0.0.1/")]})
        ),
    )
    result = await adapter.search(PublicSearchRequest("public"), timeout_seconds=1)
    assert result.sources == ()
    assert result.safe_code == "SOURCE_UNAVAILABLE"


@pytest.mark.parametrize("body", [b"not JSON", b"[]", b'{"unexpected": true}'])
@pytest.mark.asyncio
async def test_malformed_provider_response_is_sanitized(body: bytes) -> None:
    from app.modules.retrieval.adapters.exa import ExaAdapter
    from app.modules.retrieval.domain.models import PublicSearchRequest, RetrievalProviderError

    adapter = ExaAdapter(
        SecretStr("test-key"),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)),
    )
    with pytest.raises(RetrievalProviderError, match="PROVIDER_INVALID_RESPONSE"):
        await adapter.search(PublicSearchRequest("public"), timeout_seconds=1)


@pytest.mark.asyncio
async def test_date_and_domain_filters_are_enforced_on_returned_results() -> None:
    from datetime import UTC, datetime

    from app.modules.retrieval.adapters.exa import ExaAdapter
    from app.modules.retrieval.domain.models import PublicSearchRequest, ValidatedSourceFilters

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    _result(),
                    {**_result("https://example.org/old"), "publishedDate": "2020-01-01T00:00:00Z"},
                    {
                        **_result("https://excluded.example.org/"),
                        "publishedDate": "2026-10-21T00:00:00Z",
                    },
                    {**_result("https://example.org/new"), "publishedDate": "2026-10-21T00:00:00Z"},
                ]
            },
        )

    filters = ValidatedSourceFilters(
        include_domains=("example.org",),
        exclude_domains=("excluded.example.org",),
        published_after=datetime(2026, 1, 1, tzinfo=UTC),
    )
    batch = await ExaAdapter(SecretStr("test-key"), transport=httpx.MockTransport(handler)).search(
        PublicSearchRequest("public", validated_filters=filters), timeout_seconds=1
    )
    assert [source.url.value for source in batch.sources] == ["https://example.org/new"]
    assert json.loads(requests[0].content)["startPublishedDate"] == "2026-01-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_cache_policy_violation_does_not_deliver_live_content() -> None:
    from app.modules.retrieval.adapters.exa import ExaAdapter
    from app.modules.retrieval.domain.models import PublicPageRequest
    from app.modules.retrieval.domain.public_urls import validate_public_url

    adapter = ExaAdapter(
        SecretStr("test-key"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "results": [{**_result(), "text": "unexpected live content"}],
                    "statuses": [
                        {"id": "https://example.org/guide", "status": "success", "source": "live"}
                    ],
                },
            )
        ),
    )
    result = await adapter.read(
        PublicPageRequest(validate_public_url("https://example.org/guide")), timeout_seconds=1
    )
    assert result.source is None and result.safe_code == "CACHE_POLICY_VIOLATION"


@pytest.mark.asyncio
async def test_search_deduplicates_canonical_urls_and_honors_lower_result_limit() -> None:
    from app.modules.retrieval.adapters.exa import ExaAdapter
    from app.modules.retrieval.domain.models import PublicSearchRequest

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    _result("HTTPS://EXAMPLE.ORG:443/guide"),
                    _result(),
                    _result("https://example.org/extra"),
                ]
            },
        )

    batch = await ExaAdapter(SecretStr("test-key"), transport=httpx.MockTransport(handler)).search(
        PublicSearchRequest("public", limit=2), timeout_seconds=1
    )
    assert json.loads(captured[0].content)["numResults"] == 2
    assert len(batch.sources) == 1


@pytest.mark.parametrize(
    "options",
    [
        {"include_domains": ("example.org",) * 21},
        {"include_domains": ("example.org/private",)},
        {"exclude_domains": ("localhost",)},
    ],
)
def test_source_filters_are_not_raw_provider_options(options: dict[str, object]) -> None:
    from app.modules.retrieval.domain.models import ValidatedSourceFilters

    with pytest.raises(ValueError):
        ValidatedSourceFilters(**options)  # type: ignore[arg-type]
