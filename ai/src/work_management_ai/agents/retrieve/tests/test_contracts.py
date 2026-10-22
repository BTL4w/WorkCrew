from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError


def _request(**changes: object) -> dict[str, object]:
    return {
        "query": "Tìm tài liệu công khai về lập kế hoạch",
        "locale": "vi",
        "search_queries": ["public project planning guidance"],
        "requirements": [{"requirement_id": "topic", "description": "Planning guidance"}],
        **changes,
    }


def _source(**changes: object) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "evidence_id": uuid4(),
        "canonical_url": "https://example.org/planning",
        "title": "Public planning guidance",
        "excerpt": "Allow time for a review.",
        "method": "SEARCH_SNIPPET",
        "fingerprint": "a" * 64,
        "observed_at": now,
        "expires_at": now + timedelta(days=1),
        "provider_version": "exa.auto.v1",
        **changes,
    }


@pytest.mark.parametrize("locale", ["vi", "en"])
def test_valid_web_request_keeps_model_and_provider_neutral_contract(locale: str) -> None:
    from work_management_ai.agents.retrieve.contracts import RetrieveInput

    request = RetrieveInput.model_validate(_request(locale=locale))
    assert request.source_mode == "WEB"
    assert request.answer_mode == "SUMMARY"
    assert request.limits.max_provider_requests == 8
    assert request.limits.timeout_seconds == 25


@pytest.mark.parametrize(
    "changes",
    [
        {"query": "x" * 2001},
        {"query": "   "},
        {"search_queries": ["one", "two", "three"]},
        {"search_queries": ["x" * 501]},
        {"requirements": [{"requirement_id": str(i), "description": "topic"} for i in range(9)]},
        {"requirements": []},
        {"requirements": [{"requirement_id": "same", "description": "a"}] * 2},
        {"limits": {"max_provider_requests": 9}},
        {"limits": {"max_input_tokens": 12001}},
        {"limits": {"max_output_tokens": 3201}},
        {"limits": {"max_handoffs": 1}},
        {"limits": {"max_replans": 1}},
        {"limits": {"max_queries": True}},
        {"limits": {"max_queries": 1}, "search_queries": ["one", "two"]},
        {"explicit_public_urls": ["file:///etc/passwd"]},
        {"filters": {"raw_sql": "SELECT anything"}},
        {"role": "ADMIN"},
        {"activation": {"active_tracks": ["I3"]}},
        {"approved": True},
    ],
)
def test_retrieve_rejects_unknown_evidence_and_oversized_input(changes: dict[str, object]) -> None:
    from work_management_ai.agents.retrieve.contracts import RetrieveInput

    with pytest.raises(ValidationError):
        RetrieveInput.model_validate(_request(**changes))


def test_unknown_claim_evidence_is_rejected_without_losing_source_identity() -> None:
    from work_management_ai.agents.retrieve.contracts import RetrieveOutput

    source = _source()
    with pytest.raises(ValidationError, match="evidence"):
        RetrieveOutput.model_validate(
            {
                "outcome": "ANSWER_READY",
                "strategy_used": "WEB",
                "summary": {"claims": [{"text": "Review first", "evidence_ids": [uuid4()]}]},
                "evidence": [source],
                "observed_at": source["observed_at"],
            }
        )


def test_valid_summary_is_bound_to_sources_and_unknown_freshness_stays_unknown() -> None:
    from work_management_ai.agents.retrieve.contracts import RetrieveOutput

    source = _source()
    result = RetrieveOutput.model_validate(
        {
            "outcome": "ANSWER_READY",
            "strategy_used": "WEB",
            "summary": {
                "claims": [{"text": "Allow a review", "evidence_ids": [source["evidence_id"]]}]
            },
            "evidence": [source],
            "observed_at": source["observed_at"],
        }
    )
    assert result.evidence[0].published_at is None
    assert result.evidence[0].source_fetched_at is None
    assert result.usage.input_tokens is None
    assert result.usage.provider_cost_usd is None
    with pytest.raises(ValidationError):
        RetrieveOutput.model_validate({**result.model_dump(), "evidence": [source, source]})


@pytest.mark.parametrize(
    "changes",
    [
        {"permissions": {"role": "ADMIN"}},
        {"canonical_url": "javascript:alert(1)"},
        {"canonical_url": "https://user:secret@example.org/"},
        {"fingerprint": "not-a-hash"},
        {"expires_at": datetime(2020, 1, 1, tzinfo=UTC)},
        {"excerpt": "😀" * 20000},
    ],
)
def test_source_metadata_cannot_grant_authority(changes: dict[str, object]) -> None:
    from work_management_ai.agents.retrieve.contracts import WebEvidence

    with pytest.raises(ValidationError):
        WebEvidence.model_validate(_source(**changes))


def test_snippet_cannot_certify_a_page_quote() -> None:
    from work_management_ai.agents.retrieve.contracts import RetrieveOutput

    source = _source()
    claim = {
        "text": "Review first",
        "evidence_ids": [source["evidence_id"]],
        "quote": {
            "evidence_id": source["evidence_id"],
            "text": "Allow",
            "start": 0,
            "end": 5,
            "fingerprint": "a" * 64,
        },
    }
    with pytest.raises(ValidationError, match="quote"):
        RetrieveOutput.model_validate(
            {
                "outcome": "ANSWER_READY",
                "strategy_used": "WEB",
                "summary": {"claims": [claim]},
                "evidence": [source],
                "observed_at": source["observed_at"],
            }
        )


def test_required_unavailable_source_cannot_be_reported_as_complete_answer() -> None:
    from work_management_ai.agents.retrieve.contracts import RetrieveOutput

    source = _source()
    with pytest.raises(ValidationError, match="coverage"):
        RetrieveOutput.model_validate(
            {
                "outcome": "ANSWER_READY",
                "strategy_used": "WEB",
                "summary": {
                    "claims": [{"text": "Public fact", "evidence_ids": [source["evidence_id"]]}]
                },
                "evidence": [source],
                "coverage": [{"source": "DOCUMENTS", "state": "UNAVAILABLE", "required": True}],
                "observed_at": source["observed_at"],
            }
        )


def test_output_timestamp_requires_timezone_provenance() -> None:
    from work_management_ai.agents.retrieve.contracts import RetrieveOutput

    with pytest.raises(ValidationError):
        RetrieveOutput.model_validate(
            {"outcome": "NOT_FOUND", "strategy_used": "WEB", "observed_at": "2026-10-22T09:00:00"}
        )


def test_nonanswer_cannot_carry_claims() -> None:
    from work_management_ai.agents.retrieve.contracts import RetrieveOutput

    source = _source()
    with pytest.raises(ValidationError, match="non-answer"):
        RetrieveOutput.model_validate(
            {
                "outcome": "NOT_FOUND",
                "strategy_used": "WEB",
                "summary": {
                    "claims": [{"text": "Public fact", "evidence_ids": [source["evidence_id"]]}]
                },
                "evidence": [source],
                "observed_at": source["observed_at"],
            }
        )
