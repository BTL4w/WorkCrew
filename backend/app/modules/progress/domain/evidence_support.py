"""Versioned deterministic support scoring; semantic findings are assessments."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field

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


class EvidenceSupportResult(ReportingContract):
    score: Decimal | None
    warning_codes: tuple[WarningCode, ...]
    assessed_count: int
    total_count: int
    rule_version: Literal["evidence-support.v1"] = "evidence-support.v1"


class AssessmentWarning(ReportingContract):
    id: UUID
    code: WarningCode


class AssessmentProvenance(ReportingContract):
    agent_version: Literal["1.0.0"] = "1.0.0"
    workflow_version: Literal["daily-update.v1"] = "daily-update.v1"
    skill_version: Literal["1.0.0"] = "1.0.0"
    tool_version: Literal["1.0.0"] = "1.0.0"
    prompt_versions: tuple[str, ...]
    model_refs: tuple[str, ...]
    verifier_version: Literal["daily-update-grounding.v1"] = "daily-update-grounding.v1"


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


def score_support(
    claims: tuple[Claim, ...], findings: tuple[ClaimFinding, ...], coverage: SourceCoverage
) -> EvidenceSupportResult:
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
    points = {
        "SUPPORTED": Decimal(1),
        "PARTIAL": Decimal("0.5"),
        "UNSUPPORTED": Decimal(0),
        "CONTRADICTED": Decimal(0),
    }
    # Contradictions survive duplicate consolidation; otherwise use weakest assessable finding.
    scored = [
        min(points[f.finding] for f in group if f.finding in points)
        for group in groups.values()
        if any(f.finding in points for f in group)
    ]
    warnings: list[WarningCode] = []
    score = 100 * sum(scored, Decimal(0)) / len(scored) if scored else None
    if score is not None and score < 70:
        warnings.append("LOW_SUPPORT")
    if any(f.finding == "CONTRADICTED" for f in findings):
        warnings.append("CONTRADICTION")
    if (
        coverage.processed_count < coverage.total_count
        or any(
            f.finding == "UNASSESSABLE" and c.checkability for c in claims for f in (by_id[c.id],)
        )
        or (groups and not scored)
    ):
        warnings.append("INSUFFICIENT_ASSESSMENT")
    return EvidenceSupportResult(
        score=score,
        warning_codes=tuple(warnings),
        assessed_count=len(scored),
        total_count=len(groups),
    )
