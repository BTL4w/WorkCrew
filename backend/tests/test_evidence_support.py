"""Literal scoring oracles and hostile comparison-result verification."""

from collections.abc import Sequence
from decimal import Decimal
from uuid import uuid4

import pytest

from app.modules.progress.domain.daily_updates import DailyUpdateError, SelectedEvidence
from app.modules.progress.domain.evidence_support import (
    Claim,
    ClaimFinding,
    EvidenceSupportResult,
    SourceCoverage,
    score_support,
)

SOURCE = SelectedEvidence(evidence_id=uuid4(), version=1)


def inputs(kinds: Sequence[str]) -> tuple[tuple[Claim, ...], tuple[ClaimFinding, ...]]:
    claims = tuple(
        Claim(
            id=str(i),
            source_span=f"item:{i}",
            text=f"Delivered output {i}",
            evidence_refs=(SOURCE,),
        )
        for i in range(len(kinds))
    )
    findings = tuple(
        ClaimFinding(
            claim_id=str(i),
            finding=ClaimFinding.model_validate({"claim_id": str(i), "finding": kind}).finding,
            source_refs=(SOURCE,) if kind != "UNASSESSABLE" else (),
            limitation="Unreadable source" if kind == "UNASSESSABLE" else "",
        )
        for i, kind in enumerate(kinds)
    )
    return claims, findings


def score(kinds: Sequence[str]) -> EvidenceSupportResult:
    return score_support(*inputs(kinds), SourceCoverage(processed_count=1, total_count=1))


def test_exact_threshold_and_independent_contradiction_warning():
    threshold_result = score(["SUPPORTED"] * 7 + ["UNSUPPORTED"] * 3)
    assert threshold_result.score == Decimal("70")
    assert "LOW_SUPPORT" not in threshold_result.warning_codes
    partial = score(["SUPPORTED"] * 6 + ["PARTIAL"] + ["UNSUPPORTED"] * 3)
    assert partial.score == Decimal("65")
    assert partial.warning_codes == ("LOW_SUPPORT",)
    contradiction = score(["SUPPORTED"] * 8 + ["CONTRADICTED"])
    assert contradiction.score is not None and contradiction.score > 70
    assert contradiction.warning_codes == ("CONTRADICTION",)


def test_unassessable_and_incomplete_coverage_are_visible_not_passed():
    result = score(["UNASSESSABLE"])
    assert result.score is None
    assert result.assessed_count == 0 and result.total_count == 1
    assert result.warning_codes == ("INSUFFICIENT_ASSESSMENT",)
    result = score_support(*inputs(["SUPPORTED"]), SourceCoverage(processed_count=1, total_count=2))
    assert result.score == 100
    assert result.warning_codes == ("INSUFFICIENT_ASSESSMENT",)


@pytest.mark.parametrize(
    "tamper",
    ["omit", "duplicate", "unknown", "forged_source", "unjustified_exclusion", "missing_citation"],
)
def test_invalid_comparison_is_rejected(tamper: str) -> None:
    claims, findings = inputs(["SUPPORTED", "UNSUPPORTED"])
    if tamper == "omit":
        findings = findings[:1]
    if tamper == "duplicate":
        findings = (findings[0], findings[0])
    if tamper == "unknown":
        findings = (findings[0].model_copy(update={"claim_id": "foreign"}), findings[1])
    if tamper == "forged_source":
        findings = (
            findings[0].model_copy(
                update={"source_refs": (SelectedEvidence(evidence_id=uuid4(), version=1),)}
            ),
            findings[1],
        )
    if tamper == "unjustified_exclusion":
        findings = (ClaimFinding(claim_id="0", finding="UNASSESSABLE"), findings[1])
    if tamper == "missing_citation":
        findings = (findings[0].model_copy(update={"source_refs": ()}), findings[1])
    with pytest.raises(DailyUpdateError, match="INVALID_ASSESSMENT"):
        score_support(claims, findings, SourceCoverage(processed_count=1, total_count=1))


def test_duplicate_claims_cannot_inflate_score_or_remove_contradiction():
    claims, findings = inputs(["SUPPORTED", "SUPPORTED", "CONTRADICTED"])
    claims = (
        claims[0],
        claims[1].model_copy(update={"text": "  DELIVERED   OUTPUT 0 "}),
        claims[2],
    )
    result = score_support(claims, findings, SourceCoverage(processed_count=1, total_count=1))
    assert result.score == 50
    assert result.total_count == 2
    assert "CONTRADICTION" in result.warning_codes
    findings = (
        findings[0],
        findings[1].model_copy(update={"finding": "CONTRADICTED"}),
        findings[2],
    )
    assert (
        score_support(claims, findings, SourceCoverage(processed_count=1, total_count=1)).score == 0
    )
