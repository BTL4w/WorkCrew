"""Current-message provenance is necessary, never sufficient permission for egress."""

import re
from datetime import datetime
from typing import Any, cast

from app.modules.retrieval.domain.models import (
    PublicSearchRequest,
    RetrievalPolicyError,
    ValidatedSourceFilters,
)


def validate_public_query(
    intent: dict[str, Any], current_message: str, protected_terms: tuple[str, ...]
) -> tuple[PublicSearchRequest, ...]:
    queries = intent.get("search_queries")
    spans = intent.get("message_spans")
    if not isinstance(queries, (list, tuple)) or not 1 <= len(cast(tuple[Any, ...], queries)) <= 2:
        raise RetrievalPolicyError("PUBLIC_QUERY_PROVENANCE_REQUIRED")
    if not isinstance(spans, (list, tuple)) or len(cast(tuple[Any, ...], spans)) != len(
        cast(tuple[Any, ...], queries)
    ):
        raise RetrievalPolicyError("PUBLIC_QUERY_PROVENANCE_REQUIRED")
    if not re.search(r"\b(web|public|internet|search)\b|công khai|tra cứu", current_message, re.I):
        raise RetrievalPolicyError("PUBLIC_QUERY_PROVENANCE_REQUIRED")
    result: list[PublicSearchRequest] = []
    for query, span in zip(cast(list[Any], queries), cast(list[Any], spans), strict=True):
        if not isinstance(query, str) or not isinstance(span, dict):
            raise RetrievalPolicyError("PUBLIC_QUERY_PROVENANCE_REQUIRED")
        span = cast(dict[str, Any], span)
        start, end = span.get("start"), span.get("end")
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(current_message)
            or span.get("text") != query
            or current_message[start:end] != query
        ):
            raise RetrievalPolicyError("PUBLIC_QUERY_PROVENANCE_REQUIRED")
        query.encode("utf-8", errors="strict")
        if any(term.strip() and term.casefold() in query.casefold() for term in protected_terms):
            raise RetrievalPolicyError("PUBLIC_QUERY_PROTECTED_VALUE")
        if re.search(
            r"\b(sk-|api[_ -]?key|password|token|bearer|secret|confidential|internal)\b|"
            r"nội bộ|bí mật|mật khẩu|\bsk-[\w-]+|[\w.+-]+@[\w.-]+",
            query,
            re.I,
        ):
            raise RetrievalPolicyError("PUBLIC_QUERY_PROTECTED_VALUE")
        raw = intent.get("filters", {})
        if not isinstance(raw, dict) or set(cast(dict[str, Any], raw)) - {
            "include_domains",
            "exclude_domains",
            "published_after",
            "published_before",
        }:
            raise RetrievalPolicyError("PUBLIC_QUERY_PROVENANCE_REQUIRED")
        raw = cast(dict[str, Any], raw)
        for domain in (*raw.get("include_domains", ()), *raw.get("exclude_domains", ())):
            if not isinstance(domain, str) or domain not in current_message:
                raise RetrievalPolicyError("PUBLIC_QUERY_PROVENANCE_REQUIRED")
        for at in (raw.get("published_after"), raw.get("published_before")):
            if at and str(at)[:10] not in current_message:
                raise RetrievalPolicyError("PUBLIC_QUERY_PROVENANCE_REQUIRED")
        filters = ValidatedSourceFilters(
            tuple(raw.get("include_domains", ())),
            tuple(raw.get("exclude_domains", ())),
            datetime.fromisoformat(raw["published_after"]) if raw.get("published_after") else None,
            datetime.fromisoformat(raw["published_before"])
            if raw.get("published_before")
            else None,
        )
        result.append(PublicSearchRequest(query, filters))
    return tuple(result)
