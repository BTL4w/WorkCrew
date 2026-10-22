"""Stable, bounded whole-source selection. Snapshot text and identity never change."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from work_management_ai.agents.retrieve.contracts import RetrieveLimits, WebEvidence


def canonical_source_key(url: str) -> str:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").encode("idna").decode().lower()
    port = parsed.port
    address = f"[{hostname}]" if ":" in hostname else hostname
    default_port = {"http": 80, "https": 443}.get(parsed.scheme.lower())
    host = address if port is None or port == default_port else f"{address}:{port}"
    return urlunsplit(
        (
            parsed.scheme.lower(),
            host,
            parsed.path or "/",
            urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True))),
            "",
        )
    )


def source_is_valid(source: WebEvidence, *, now: datetime | None = None) -> bool:
    try:
        value = WebEvidence.model_validate(source.model_dump())
        return (
            value.expires_at > (now or datetime.now(UTC))
            and hashlib.sha256(value.excerpt.encode()).hexdigest() == value.fingerprint
        )
    except (ValueError, TypeError, UnicodeError):
        return False


def synthesis_sources(sources: tuple[WebEvidence, ...]) -> list[dict[str, str]]:
    return [
        {
            "evidence_id": str(item.evidence_id),
            "method": item.method,
            "excerpt": item.excerpt,
            "fingerprint": item.fingerprint,
        }
        for item in sources
    ]


def source_token_bound(sources: tuple[WebEvidence, ...]) -> int:
    # Conservative byte-level token bound, including source JSON framing. URLs,
    # provider metadata, authorization and timestamps are not model-generated fields.
    return len(
        json.dumps(synthesis_sources(sources), ensure_ascii=False, separators=(",", ":")).encode()
    )


@dataclass(frozen=True, slots=True)
class EvidencePack:
    sources: tuple[WebEvidence, ...]
    token_upper_bound: int
    limitations: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> str:
        payload = [source.model_dump(mode="json") for source in self.sources]
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def pack_web_evidence(sources: tuple[WebEvidence, ...], limits: RetrieveLimits) -> EvidencePack:
    limits = RetrieveLimits.model_validate(limits.model_dump())
    selected: tuple[WebEvidence, ...] = ()
    seen_urls: set[str] = set()
    seen_ids: set[object] = set()
    warnings: list[str] = []
    for source in sources:
        if not source_is_valid(source):
            warnings.append("SOURCE_INVALID")
            continue
        key = canonical_source_key(source.canonical_url)
        if key in seen_urls or source.evidence_id in seen_ids:
            continue
        if len(selected) >= limits.max_sources:
            warnings.append("SOURCE_LIMIT_REACHED")
            continue
        candidate = (*selected, source)
        if source_token_bound(candidate) > limits.max_evidence_tokens:
            warnings.append("EVIDENCE_PACK_LIMIT")
            continue
        selected = candidate
        seen_urls.add(key)
        seen_ids.add(source.evidence_id)
        if source.published_at is None or source.source_fetched_at is None:
            warnings.append("SOURCE_FRESHNESS_UNKNOWN")
    return EvidencePack(
        selected, source_token_bound(selected) if selected else 0, tuple(dict.fromkeys(warnings))
    )
