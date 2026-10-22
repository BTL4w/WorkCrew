"""Hand-checked public DTO fixtures, independent of projection builders."""

from typing import Any

SOURCE_ID = "11111111-1111-4111-8111-111111111111"
RUN_ID = "22222222-2222-4222-8222-222222222222"


def public_answer() -> dict[str, Any]:
    return dict(
        kind="retrieval_answer",
        schema_version="1.0",
        context_run_id=RUN_ID,
        outcome="ANSWER_READY",
        claims=[dict(text="Review the goal and constraints.", evidence_ids=[SOURCE_ID])],
        sources=[
            dict(
                evidence_id=SOURCE_ID,
                title="Public planning guide",
                canonical_url="https://example.org/guide",
                method="SEARCH_SNIPPET",
                excerpt="Review the goal and constraints.",
                published_at=None,
                source_fetched_at=None,
            )
        ],
        limitations=["SOURCE_FRESHNESS_UNKNOWN"],
        observed_at="2026-10-23T01:00:00Z",
        availability="AVAILABLE",
    )
