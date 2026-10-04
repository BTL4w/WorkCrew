"""Fail closed on missing, duplicated or contradictory semantic verdicts."""

from work_management_ai.agents.reporting.contracts import (
    ReportingNarrative,
    SemanticVerdict,
    TextBlock,
)
from work_management_ai.runtime.contracts import VerifierResult


def verify_grounding(narrative: ReportingNarrative, verdict: SemanticVerdict) -> VerifierResult:
    expected = {b.id for b in narrative.blocks if isinstance(b, TextBlock)}
    actual = {v.block_id for v in verdict.claim_verdicts}
    codes: set[str] = set()
    if actual != expected or len(actual) != len(verdict.claim_verdicts):
        codes.add("SEMANTIC_CLAIM_COVERAGE")
    if (
        not verdict.passed
        or verdict.safe_codes
        or any(
            not (v.grounded and v.quantities_bound and v.no_unsupported_cause_or_forecast)
            or v.safe_codes
            for v in verdict.claim_verdicts
        )
    ):
        codes.add("SEMANTIC_REJECTED")
    return VerifierResult(
        verifier_id="reporting_grounding",
        verifier_version="1.0.0",
        passed=not codes,
        safe_codes=tuple(sorted(codes)),
    )
