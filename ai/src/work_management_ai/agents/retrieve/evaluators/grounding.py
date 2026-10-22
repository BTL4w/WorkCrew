"""Verify identities, URLs and exact page quotes without pretending to prove meaning."""

import re

from work_management_ai.agents.retrieve.contracts import GroundedSummary
from work_management_ai.agents.retrieve.evidence import EvidencePack, source_is_valid
from work_management_ai.runtime.contracts import VerifierResult


def verify_grounded_summary(summary: GroundedSummary, evidence: EvidencePack) -> VerifierResult:
    codes: list[str] = []
    try:
        summary = GroundedSummary.model_validate(summary.model_dump())
    except ValueError:
        codes.append("SUMMARY_SCHEMA_INVALID")
    sources = {source.evidence_id: source for source in evidence.sources}
    urls = {source.canonical_url for source in evidence.sources}
    if len(sources) != len(evidence.sources) or any(
        not source_is_valid(s) for s in evidence.sources
    ):
        codes.append("SOURCE_INVALID")
    if not summary.claims or summary.insufficient_evidence:
        codes.append("INSUFFICIENT_EVIDENCE")
    for claim in summary.claims:
        if not claim.evidence_ids or not set(claim.evidence_ids).issubset(sources):
            codes.append("CITATION_ID_INVALID")
        if any(
            url.rstrip(".,;!?)") not in urls
            for url in re.findall(r"https?://[^\s<>\]]+", claim.text)
        ):
            codes.append("CITATION_URL_INVALID")
        if claim.quote is not None:
            quote = claim.quote
            source = sources.get(quote.evidence_id)
            if (
                source is None
                or source.method != "PAGE_CONTENT"
                or quote.evidence_id not in claim.evidence_ids
                or source.fingerprint != quote.fingerprint
                or quote.start < 0
                or quote.end > len(source.excerpt)
                or quote.end - quote.start != len(quote.text)
                or source.excerpt[quote.start : quote.end] != quote.text
            ):
                codes.append("QUOTE_BINDING_INVALID")
    return VerifierResult(
        verifier_id="retrieve_grounding",
        verifier_version="1.0.0",
        passed=not codes,
        safe_codes=tuple(dict.fromkeys(codes)),
    )
