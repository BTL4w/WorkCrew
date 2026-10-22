from typing import Any

import pytest


def intent(query: str = "public planning") -> dict[str, Any]:
    return {
        "search_queries": [query],
        "message_spans": [{"start": 22, "end": 22 + len(query), "text": query}],
        "requirements": [{"requirement_id": "topic", "description": query}],
        "source_mode": "WEB",
        "answer_mode": "SUMMARY",
    }


def test_current_public_span_is_allowed():
    from app.modules.retrieval.application.query_policy import validate_public_query

    requests = validate_public_query(intent(), "Search public web for public planning", ())
    assert requests[0].query == "public planning"


@pytest.mark.parametrize(
    "query,protected",
    [
        ("Project Orion", ("Project Orion",)),
        ("sk-proj-verysecret12345678", ()),
        ("boss@example.com", ()),
        ("private invented topic", ()),
    ],
)
def test_secret_or_unproven_query_is_refused(query: str, protected: tuple[str, ...]):
    from app.modules.retrieval.application.query_policy import validate_public_query

    with pytest.raises(ValueError):
        validate_public_query(
            intent(query),
            "Search public web for "
            + ("public planning" if query == "private invented topic" else query),
            protected,
        )
