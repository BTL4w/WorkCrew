"""AI-authored support judgments with deterministic validation and warnings."""

from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator

from app.modules.progress.domain.daily_updates import (
    DailyUpdateError,
    ReportingContract,
    SelectedEvidence,
)

FindingKind = Literal["SUPPORTED", "PARTIAL", "UNSUPPORTED", "CONTRADICTED", "UNASSESSABLE"]
WarningCode = Literal["LOW_SUPPORT", "CONTRADICTION", "INSUFFICIENT_ASSESSMENT"]


class Claim(ReportingContract):
    id: str = Field(min_length=1, max_length=128)
    source_span: str
    category: Literal["WORK", "OUTPUT", "ACCEPTANCE_CRITERION"] = "WORK"
    checkability: bool = True
    text: str = Field(min_length=1, max_length=4000)
    task_id: UUID | None = None
    evidence_refs: tuple[SelectedEvidence, ...] = ()


class ClaimFinding(ReportingContract):
    claim_id: str
    finding: FindingKind
    source_refs: tuple[SelectedEvidence, ...] = ()
    limitation: str = Field(default="", max_length=2000)


class SourceCoverage(ReportingContract):
    processed_count: int = Field(ge=0)
    total_count: int = Field(ge=0)


class EvidenceJudgment(ReportingContract):
    score: Decimal | None = Field(ge=0, le=100, allow_inf_nan=False)
    rationale: str = Field(min_length=1, max_length=4000)
    recommendations: tuple[Annotated[str, Field(min_length=1, max_length=1000)], ...] = Field(
        default=(), max_length=5
    )

    @field_validator("rationale")
    @classmethod
    def nonblank_rationale(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Assessment rationale is required")
        return value.strip()


class EvidenceSupportResult(ReportingContract):
    score: Decimal | None
    warning_codes: tuple[WarningCode, ...]
    assessed_count: int
    total_count: int
    # Legacy defaults preserve the meaning of immutable v1 JSON snapshots.
    rule_version: Literal["evidence-support.v1", "evidence-support.ai.v2"] = "evidence-support.v1"
    scoring_method: Literal["RULE_BASED", "AI"] = "RULE_BASED"
    rationale: str = ""
    recommendations: tuple[str, ...] = ()


class AssessmentWarning(ReportingContract):
    id: UUID
    code: WarningCode


class AssessmentProvenance(ReportingContract):
    agent_version: Literal["1.0.0"] = "1.0.0"
    workflow_version: Literal["daily-update.v1"] = "daily-update.v1"
    skill_version: Literal["1.0.0", "1.1.0"] = "1.1.0"
    tool_version: Literal["1.0.0"] = "1.0.0"
    prompt_versions: tuple[str, ...]
    model_refs: tuple[str, ...]
    verifier_version: Literal["daily-update-grounding.v1", "daily-update-grounding.v2"] = (
        "daily-update-grounding.v2"
    )


class DraftAssessment(ReportingContract):
    provenance: AssessmentProvenance | None = None
    id: UUID | None = None
    draft_id: UUID
    draft_version: int
    state: Literal["PENDING", "READY", "UNAVAILABLE", "NOT_ASSESSED_NO_EVIDENCE", "STALE"]
    result: EvidenceSupportResult | None = None
    claims: tuple[Claim, ...] = ()
    findings: tuple[ClaimFinding, ...] = ()
    coverage: SourceCoverage = SourceCoverage(processed_count=0, total_count=0)
    warnings: tuple[AssessmentWarning, ...] = ()
    limitation: str = ""


class AssessmentJobRef(ReportingContract):
    id: UUID
    draft_id: UUID
    state: str


def evaluate_support(
    claims: tuple[Claim, ...],
    findings: tuple[ClaimFinding, ...],
    coverage: SourceCoverage,
    judgment: EvidenceJudgment,
) -> EvidenceSupportResult:
    judgment = EvidenceJudgment.model_validate(judgment.model_dump())
    if coverage.processed_count > coverage.total_count or len({c.id for c in claims}) != len(
        claims
    ):
        raise DailyUpdateError("INVALID_ASSESSMENT", 422)
    if len({f.claim_id for f in findings}) != len(findings) or {c.id for c in claims} != {
        f.claim_id for f in findings
    }:
        raise DailyUpdateError("INVALID_ASSESSMENT", 422)
    by_id = {f.claim_id: f for f in findings}
    groups: dict[tuple[UUID | None, str], list[ClaimFinding]] = {}
    for claim in claims:
        finding = by_id[claim.id]
        if (
            not set(finding.source_refs).issubset(set(claim.evidence_refs))
            or (finding.finding == "UNASSESSABLE" and not finding.limitation.strip())
            or (
                finding.finding in {"SUPPORTED", "PARTIAL", "CONTRADICTED"}
                and not finding.source_refs
            )
        ):
            raise DailyUpdateError("INVALID_ASSESSMENT", 422)
        if claim.checkability:
            groups.setdefault((claim.task_id, " ".join(claim.text.casefold().split())), []).append(
                finding
            )
    assessed_count = sum(
        any(f.finding != "UNASSESSABLE" for f in group) for group in groups.values()
    )
    if judgment.score is not None and (not assessed_count or not coverage.processed_count):
        raise DailyUpdateError("INVALID_ASSESSMENT", 422)
    warnings: list[WarningCode] = []
    score = judgment.score
    if score is not None and score < 70:
        warnings.append("LOW_SUPPORT")
    if any(f.finding == "CONTRADICTED" for f in findings):
        warnings.append("CONTRADICTION")
    if (
        coverage.processed_count < coverage.total_count
        or any(
            f.finding == "UNASSESSABLE" and c.checkability for c in claims for f in (by_id[c.id],)
        )
        or score is None
    ):
        warnings.append("INSUFFICIENT_ASSESSMENT")
    return EvidenceSupportResult(
        score=score,
        warning_codes=tuple(warnings),
        assessed_count=assessed_count,
        total_count=len(groups),
        rule_version="evidence-support.ai.v2",
        scoring_method="AI",
        rationale=judgment.rationale,
        recommendations=judgment.recommendations,
    )
