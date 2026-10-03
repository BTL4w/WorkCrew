"""AI-authored scores with deterministic validation and warning policy."""

from collections.abc import Sequence
from decimal import Decimal
from uuid import uuid4

import pytest

from app.modules.progress.domain.daily_updates import DailyUpdateError, SelectedEvidence
from app.modules.progress.domain.evidence_support import (
    Claim,
    ClaimFinding,
    EvidenceJudgment,
    EvidenceSupportResult,
    SourceCoverage,
    evaluate_support,
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


def score(kinds: Sequence[str], value: str | None = "83") -> EvidenceSupportResult:
    return evaluate_support(
        *inputs(kinds),
        SourceCoverage(processed_count=1, total_count=1),
        EvidenceJudgment(
            score=Decimal(value) if value is not None else None,
            rationale="Evidence-based assessment.",
            recommendations=(),
        ),
    )


def test_model_score_is_preserved_instead_of_derived_from_labels():
    assert score(["SUPPORTED"], "83").score == Decimal("83")
    assert score(["SUPPORTED"], "37").score == Decimal("37")
    assert score(["PARTIAL"], "91").score == Decimal("91")
    assert score(["SUPPORTED"]).scoring_method == "AI"
    assert score(["SUPPORTED"]).rule_version == "evidence-support.ai.v2"


def test_exact_threshold_and_independent_contradiction_warning():
    assert "LOW_SUPPORT" not in score(["PARTIAL"], "70").warning_codes
    assert score(["SUPPORTED"], "69.99").warning_codes == ("LOW_SUPPORT",)
    assert score(["CONTRADICTED"], "92").warning_codes == ("CONTRADICTION",)


def test_unassessable_and_incomplete_coverage_are_visible_not_passed():
    result = score(["UNASSESSABLE"], None)
    assert result.score is None
    assert result.assessed_count == 0 and result.total_count == 1
    assert result.warning_codes == ("INSUFFICIENT_ASSESSMENT",)
    with pytest.raises(DailyUpdateError, match="INVALID_ASSESSMENT"):
        score(["UNASSESSABLE"], "100")
    result = evaluate_support(
        *inputs(["SUPPORTED"]),
        SourceCoverage(processed_count=1, total_count=2),
        EvidenceJudgment(score=Decimal("83"), rationale="Partial source coverage."),
    )
    assert result.score == 83
    assert result.warning_codes == ("INSUFFICIENT_ASSESSMENT",)


@pytest.mark.parametrize("value", ["-1", "101", "NaN", "Infinity", "not a number"])
def test_invalid_model_score_is_rejected(value: str):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EvidenceJudgment.model_validate({"score": value, "rationale": "Assessment"})


@pytest.mark.parametrize("locale", ["vi", "en"])
def test_model_rationale_and_advice_survive(locale: str):
    reason = "Ảnh hỗ trợ phần khảo sát." if locale == "vi" else "The image supports the survey."
    advice = "Bổ sung ngày khảo sát." if locale == "vi" else "Add the survey date."
    result = evaluate_support(
        *inputs(["SUPPORTED"]),
        SourceCoverage(processed_count=1, total_count=1),
        EvidenceJudgment(score=Decimal("83"), rationale=reason, recommendations=(advice,)),
    )
    assert result.rationale == reason and result.recommendations == (advice,)


def test_blank_rationale_is_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EvidenceJudgment(score=Decimal("83"), rationale="   ")


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
        evaluate_support(
            claims,
            findings,
            SourceCoverage(processed_count=1, total_count=1),
            EvidenceJudgment(score=Decimal("83"), rationale="Model judgment."),
        )


def test_duplicate_claims_do_not_change_model_score_or_remove_contradiction():
    claims, findings = inputs(["SUPPORTED", "SUPPORTED", "CONTRADICTED"])
    claims = (
        claims[0],
        claims[1].model_copy(update={"text": "  DELIVERED   OUTPUT 0 "}),
        claims[2],
    )
    result = evaluate_support(
        claims,
        findings,
        SourceCoverage(processed_count=1, total_count=1),
        EvidenceJudgment(score=Decimal("83"), rationale="Model judgment."),
    )
    assert result.score == 83
    assert result.total_count == 2
    assert "CONTRADICTION" in result.warning_codes
    findings = (
        findings[0],
        findings[1].model_copy(update={"finding": "CONTRADICTED"}),
        findings[2],
    )
    assert (
        evaluate_support(
            claims,
            findings,
            SourceCoverage(processed_count=1, total_count=1),
            EvidenceJudgment(score=Decimal("83"), rationale="Model judgment."),
        ).score
        == 83
    )


def test_legacy_snapshot_keeps_its_original_score_and_origin():
    result = EvidenceSupportResult.model_validate(
        {
            "score": "65",
            "warning_codes": ["LOW_SUPPORT"],
            "assessed_count": 9,
            "total_count": 10,
            "rule_version": "evidence-support.v1",
        }
    )
    assert result.score == 65 and result.scoring_method == "RULE_BASED"
    assert result.rationale == "" and result.recommendations == ()
