"""Citation identity is structural; these cases make no semantic-quality claim."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from work_management_ai.agents.retrieve.contracts import (
    GroundedClaim,
    GroundedSummary,
    QuoteLocator,
    RetrieveLimits,
)
from work_management_ai.agents.retrieve.tests.fixtures import source


def test_pack_deduplicates_without_altering_snapshot_text_or_hash() -> None:
    from work_management_ai.agents.retrieve.evidence import pack_web_evidence

    first = source()
    duplicate = source(url=first.canonical_url)
    extra = source("Another source.", url="https://other.example/guide")
    pack = pack_web_evidence((first, duplicate, extra), RetrieveLimits())
    assert pack.sources == (first, extra)
    assert pack.sources[0].excerpt == "Review the public plan before choosing a deadline."


def test_pack_skips_whole_oversized_sources_and_never_exceeds_requested_bound() -> None:
    from work_management_ai.agents.retrieve.evidence import pack_web_evidence

    giant = source("x" * 8000)
    small = source("A small source.", url="https://other.example/guide")
    pack = pack_web_evidence((giant, small), RetrieveLimits(max_evidence_tokens=200, max_sources=1))
    assert pack.sources == (small,)
    assert pack.token_upper_bound <= 200
    assert "EVIDENCE_PACK_LIMIT" in pack.limitations


def test_pack_never_retains_more_than_eight_sources() -> None:
    from work_management_ai.agents.retrieve.evidence import pack_web_evidence

    sources = tuple(source("short", url=f"https://example.org/{i}") for i in range(10))
    pack = pack_web_evidence(sources, RetrieveLimits())
    assert len(pack.sources) == 8 and pack.token_upper_bound <= 4000


@pytest.mark.parametrize("kind", ["hash", "expired", "url"])
def test_untrusted_source_copies_are_checked_before_packing(kind: str) -> None:
    from work_management_ai.agents.retrieve.evidence import pack_web_evidence

    original = source()
    changes = {
        "hash": {"fingerprint": "f" * 64},
        "expired": {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
        "url": {"canonical_url": "http://127.0.0.1/"},
    }[kind]
    pack = pack_web_evidence((original.model_copy(update=changes),), RetrieveLimits())
    assert pack.sources == () and "SOURCE_INVALID" in pack.limitations


@pytest.mark.parametrize(
    "kind", ["unknown-id", "wrong-hash", "wrong-span", "snippet-quote", "invented-url"]
)
def test_grounding_rejects_invalid_bindings_and_unverified_exact_quotes(kind: str) -> None:
    from work_management_ai.agents.retrieve.evaluators.grounding import verify_grounded_summary
    from work_management_ai.agents.retrieve.evidence import pack_web_evidence

    page = source(
        "Review first.", method="SEARCH_SNIPPET" if kind == "snippet-quote" else "PAGE_CONTENT"
    )
    quote = QuoteLocator(
        evidence_id=page.evidence_id, fingerprint=page.fingerprint, text="Review", start=0, end=6
    )
    if kind == "wrong-hash":
        quote = quote.model_copy(update={"fingerprint": "f" * 64})
    if kind == "wrong-span":
        quote = quote.model_copy(update={"start": 1, "end": 7})
    claim = GroundedClaim(text="Review first.", evidence_ids=(page.evidence_id,), quote=quote)
    if kind == "unknown-id":
        claim = claim.model_copy(update={"evidence_ids": (uuid4(),)})
    if kind == "invented-url":
        claim = claim.model_copy(update={"text": "See https://invented.example/source"})
    verdict = verify_grounded_summary(
        GroundedSummary(claims=(claim,)), pack_web_evidence((page,), RetrieveLimits())
    )
    assert verdict.passed is False and verdict.safe_codes


def test_exact_unicode_quote_is_bound_to_the_original_page_offsets() -> None:
    from work_management_ai.agents.retrieve.evaluators.grounding import verify_grounded_summary
    from work_management_ai.agents.retrieve.evidence import pack_web_evidence

    page = source("Xin chào. Đọc kế hoạch.", method="PAGE_CONTENT")
    quote = QuoteLocator(
        evidence_id=page.evidence_id, fingerprint=page.fingerprint, text="Đọc", start=10, end=13
    )
    summary = GroundedSummary(
        claims=(GroundedClaim(text="Đọc kế hoạch.", evidence_ids=(page.evidence_id,), quote=quote),)
    )
    assert verify_grounded_summary(summary, pack_web_evidence((page,), RetrieveLimits())).passed


def test_more_than_twelve_claims_cannot_bypass_structural_verifier() -> None:
    from work_management_ai.agents.retrieve.evaluators.grounding import verify_grounded_summary
    from work_management_ai.agents.retrieve.evidence import pack_web_evidence

    page = source()
    claim = GroundedClaim(text="Review first.", evidence_ids=(page.evidence_id,))
    summary = GroundedSummary.model_construct(
        claims=(claim,) * 13, limitations=(), insufficient_evidence=False
    )
    assert not verify_grounded_summary(summary, pack_web_evidence((page,), RetrieveLimits())).passed


def test_distinct_nondefault_port_urls_are_not_deduplicated() -> None:
    from work_management_ai.agents.retrieve.evidence import pack_web_evidence

    first = source("First.", url="https://example.org:80/guide")
    second = source("Second.", url="https://example.org/guide")
    assert pack_web_evidence((first, second), RetrieveLimits()).sources == (first, second)
