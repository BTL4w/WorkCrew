"""Bounded assessment use case; model comparison remains a Task 8 adapter."""

import asyncio
import hashlib
from contextlib import AbstractAsyncContextManager
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import Field

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.progress.domain.daily_updates import (
    DailyUpdateDraft,
    DailyUpdateError,
    ReportingContract,
    SelectedEvidence,
)
from app.modules.progress.domain.evidence_support import (
    AssessmentJobRef,
    AssessmentWarning,
    Claim,
    ClaimFinding,
    DraftAssessment,
    SourceCoverage,
    score_support,
)


class OriginalSource(ReportingContract):
    evidence_id: UUID
    version: int
    sha256: str
    mime_type: str


class EvidenceComparisonResult(ReportingContract):
    findings: tuple[ClaimFinding, ...]
    # Trusted adapter receipts for wholly processed originals, never model-owned counters.
    processed_sources: tuple[SelectedEvidence, ...]


class ComparisonBudget(ReportingContract):
    timeout_seconds: float = Field(default=20, gt=0, le=60)
    max_claims: int = 100
    max_sources: int = 10


class EvidenceComparisonPort(Protocol):
    async def compare(
        self,
        claims: tuple[Claim, ...],
        original_sources: tuple[OriginalSource, ...],
        budget: ComparisonBudget,
    ) -> EvidenceComparisonResult: ...


class UnavailableComparison:
    async def compare(
        self,
        claims: tuple[Claim, ...],
        original_sources: tuple[OriginalSource, ...],
        budget: ComparisonBudget,
    ) -> EvidenceComparisonResult:
        raise RuntimeError("Comparison adapter not active until Task 8")


class AssessmentRepository(Protocol):
    async def authenticate(self) -> None: ...
    async def draft(self, draft_id: UUID) -> DailyUpdateDraft: ...
    async def replay(
        self, operation: str, key: str, fingerprint: str
    ) -> dict[str, object] | None: ...
    async def remember(
        self, operation: str, key: str, fingerprint: str, result: dict[str, object]
    ) -> None: ...
    async def audit(
        self,
        action: str,
        request_id: str,
        key: str | None,
        resource_id: UUID | None,
        code: str | None = None,
    ) -> None: ...
    async def start(
        self, draft: DailyUpdateDraft
    ) -> tuple[DraftAssessment, tuple[OriginalSource, ...]]: ...
    async def finish(self, draft: DailyUpdateDraft, result: DraftAssessment) -> DraftAssessment: ...
    async def current(self, draft: DailyUpdateDraft) -> DraftAssessment: ...


class AssessmentTransactions(Protocol):
    def __call__(
        self, actor: AuthenticatedActor
    ) -> AbstractAsyncContextManager[AssessmentRepository]: ...


class AssessmentService:
    def __init__(
        self,
        transactions: AssessmentTransactions,
        comparison: EvidenceComparisonPort | None = None,
        budget: ComparisonBudget | None = None,
    ):
        self.transactions = transactions
        self.comparison = comparison or UnavailableComparison()
        self.budget = budget or ComparisonBudget()

    async def assess(
        self,
        actor: AuthenticatedActor,
        draft_id: UUID,
        expected_version: int,
        key: str,
        request_id: str,
    ) -> AssessmentJobRef:
        from app.modules.progress.application.daily_update_service import DailyUpdateService

        fingerprint = hashlib.sha256(f"{draft_id}:{expected_version}".encode()).hexdigest()
        try:
            DailyUpdateService.validate_key(key)
            async with self.transactions(actor) as repo:
                await repo.authenticate()
                replay = await repo.replay("daily_update.assess", key, fingerprint)
                if replay is not None:
                    return AssessmentJobRef.model_validate(replay)
                draft = await repo.draft(draft_id)
                if draft.version != expected_version or draft.confirmed_update_id:
                    raise DailyUpdateError("STALE_DRAFT")
                pending, sources = await repo.start(draft)
                assert pending.id is not None
                ref = AssessmentJobRef(id=pending.id, draft_id=draft_id, state=pending.state)
                await repo.remember(
                    "daily_update.assess", key, fingerprint, ref.model_dump(mode="json")
                )
                await repo.audit("daily_update.assessment_requested", request_id, key, draft_id)
            # Release transaction/locks before external work; committed PENDING is pollable.
            claims = tuple(
                Claim(
                    id=f"{i}:{line}",
                    source_span=f"items[{i}].done_text:line:{line}",
                    text=text,
                    task_id=item.task_id,
                    evidence_refs=item.evidence_refs,
                )
                for i, item in enumerate(draft.items)
                for line, text in enumerate(item.done_text.splitlines())
                if text.strip()
            )
            if not sources:
                terminal = pending.model_copy(
                    update={"state": "NOT_ASSESSED_NO_EVIDENCE", "claims": claims}
                )
            else:
                try:
                    if (
                        len(claims) > self.budget.max_claims
                        or len(sources) > self.budget.max_sources
                    ):
                        raise DailyUpdateError("ASSESSMENT_BUDGET", 422)
                    comparison = await asyncio.wait_for(
                        self.comparison.compare(claims, sources, self.budget),
                        timeout=self.budget.timeout_seconds,
                    )
                    # Revalidate even in-process adapters; untyped objects grant nothing.
                    comparison = EvidenceComparisonResult.model_validate(comparison.model_dump())
                    expected_sources = {
                        SelectedEvidence(evidence_id=s.evidence_id, version=s.version)
                        for s in sources
                    }
                    processed = set(comparison.processed_sources)
                    if len(processed) != len(
                        comparison.processed_sources
                    ) or not processed.issubset(expected_sources):
                        raise DailyUpdateError("INVALID_ASSESSMENT", 422)
                    findings = comparison.findings
                    coverage = SourceCoverage(
                        processed_count=len(processed), total_count=len(sources)
                    )
                    result = score_support(claims, findings, coverage)
                    warnings = tuple(
                        AssessmentWarning(id=uuid4(), code=code) for code in result.warning_codes
                    )
                    terminal = DraftAssessment(
                        id=pending.id,
                        draft_id=draft_id,
                        draft_version=draft.version,
                        state="READY",
                        result=result,
                        claims=claims,
                        findings=findings,
                        coverage=coverage,
                        warnings=warnings,
                    )
                except Exception:
                    terminal = pending.model_copy(
                        update={
                            "state": "UNAVAILABLE",
                            "claims": claims,
                            "limitation": "COMPARISON_UNAVAILABLE",
                        }
                    )
            async with self.transactions(actor) as repo:
                await repo.authenticate()
                terminal = await repo.finish(draft, terminal)
                await repo.audit("daily_update.assessment_finished", request_id, key, draft_id)
            return ref
        except DailyUpdateError as error:
            async with self.transactions(actor) as repo:
                await repo.audit(
                    "daily_update.rejected",
                    request_id,
                    key if key and len(key) <= 128 else None,
                    draft_id,
                    error.code,
                )
            raise

    async def get_current(self, actor: AuthenticatedActor, draft_id: UUID) -> DraftAssessment:
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            draft = await repo.draft(draft_id)
            return await repo.current(draft)
