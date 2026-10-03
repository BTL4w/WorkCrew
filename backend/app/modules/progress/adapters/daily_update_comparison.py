"""Compare typed report claims against authorized original image bytes."""

import hashlib
import json
import re
import unicodedata
from dataclasses import replace
from typing import Literal, Protocol, cast
from uuid import UUID

from pydantic import Field

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.progress.application.assessment_service import (
    ComparisonBudget,
    EvidenceComparisonResult,
    OriginalSource,
)
from app.modules.progress.domain.daily_updates import ReportingContract, SelectedEvidence
from app.modules.progress.domain.evidence import (
    MAX_EVIDENCE_BYTES,
    AuthorizedEvidenceStream,
    EvidenceVersionRef,
)
from app.modules.progress.domain.evidence_support import (
    AssessmentProvenance,
    Claim,
    ClaimFinding,
    EvidenceJudgment,
    SourceCoverage,
    evaluate_support,
)
from work_management_ai.agents.daily_update.prompts.system_v1 import (
    CLAIM_INVENTORY_V1,
    COMPARE_ORIGINALS_V2,
)
from work_management_ai.model_gateway.contracts import (
    ModelGateway,
    ModelImagePart,
    ModelMessage,
    StructuredModelRequest,
)
from work_management_ai.runtime.daily_update_budget import (
    DAILY_MODEL_SCOPE,
    BudgetScope,
    daily_model_scope,
)


class ClaimAtom(ReportingContract):
    source_claim_id: str
    text: str = Field(min_length=1, max_length=4000)
    category: Literal["WORK", "OUTPUT", "ACCEPTANCE_CRITERION"]
    checkability: bool


class ClaimInventory(ReportingContract):
    claims: tuple[ClaimAtom, ...] = Field(min_length=1, max_length=100)


class ComparisonOutput(EvidenceJudgment):
    findings: tuple[ClaimFinding, ...] = Field(max_length=100)


class OriginalReader(Protocol):
    async def open_version(
        self, actor: AuthenticatedActor, ref: EvidenceVersionRef, request_id: str
    ) -> AuthorizedEvidenceStream: ...


class OriginalEvidenceComparison:
    def __init__(
        self,
        *,
        gateway: ModelGateway,
        evidence: OriginalReader,
        actor: AuthenticatedActor,
        run_id: UUID | None = None,
    ) -> None:
        self.gateway = gateway
        self.evidence = evidence
        self.actor = actor
        self.run_id = run_id

    async def compare(
        self,
        claims: tuple[Claim, ...],
        original_sources: tuple[OriginalSource, ...],
        budget: ComparisonBudget,
    ) -> EvidenceComparisonResult:
        if any(s.mime_type not in {"image/jpeg", "image/png"} for s in original_sources):
            raise ValueError("ORIGINAL_FORMAT_UNSUPPORTED")
        if len(original_sources) > budget.max_sources or len(claims) > budget.max_claims:
            raise ValueError("ORIGINAL_CONTEXT_LIMIT")
        current = DAILY_MODEL_SCOPE.get()
        if current is None:
            if self.run_id is None:
                raise ValueError("ASSESSMENT_RUN_SCOPE_REQUIRED")
            current = BudgetScope(
                organization_id=self.actor.organization_id,
                membership_id=self.actor.membership_id,
                run_id=self.run_id,
            )
        if (
            current.organization_id != self.actor.organization_id
            or current.membership_id != self.actor.membership_id
        ):
            raise ValueError("COMPARISON_ACTOR_SCOPE_MISMATCH")
        task_contexts = {
            context.task_id: context
            for source in original_sources
            for context in source.task_contexts
        }
        with daily_model_scope(current):
            inventory = await self.gateway.generate_structured(
                StructuredModelRequest(
                    invocation_key="daily_update.claims",
                    messages=(
                        ModelMessage(
                            role="system",
                            content=CLAIM_INVENTORY_V1,
                        ),
                        ModelMessage(
                            role="user",
                            content=json.dumps(
                                {
                                    "report_lines": [c.model_dump(mode="json") for c in claims],
                                    "task_context": [
                                        c.model_dump(mode="json") for c in task_contexts.values()
                                    ],
                                }
                            ),
                        ),
                    ),
                    output_schema=ClaimInventory,
                    timeout_seconds=min(60, budget.timeout_seconds),
                    max_output_tokens=1000,
                )
            )
        originals = {c.id: c for c in claims}
        atoms = inventory.parsed.claims
        if {c.source_claim_id for c in atoms} != set(originals):
            raise ValueError("INCOMPLETE_CLAIM_INVENTORY")
        # Every meaningful character must belong to a grounded span. Merely
        # representing a line ID lets the model silently omit unsupported clauses.
        for source in claims:
            covered = [False] * len(source.text)
            seen: set[str] = set()
            for atom in atoms:
                if atom.source_claim_id != source.id:
                    continue
                if atom.text in seen:
                    raise ValueError("DUPLICATE_CLAIM_INVENTORY")
                seen.add(atom.text)
                for match in re.finditer(re.escape(atom.text), source.text):
                    covered[match.start() : match.end()] = [True] * len(atom.text)
            if any(
                not covered[index]
                and (char.isalnum() or unicodedata.category(char).startswith("S"))
                for index, char in enumerate(source.text)
            ):
                raise ValueError("INCOMPLETE_CLAIM_INVENTORY")
        resolved: list[Claim] = []
        for index, atom in enumerate(atoms):
            source = originals[atom.source_claim_id]
            if atom.text not in source.text:
                raise ValueError("UNGROUNDED_CLAIM_INVENTORY")
            resolved.append(
                Claim(
                    id=f"c{index}",
                    source_span=source.source_span,
                    text=atom.text,
                    category=atom.category,
                    checkability=atom.checkability,
                    task_id=source.task_id,
                    evidence_refs=source.evidence_refs,
                )
            )
        images: list[ModelImagePart] = []
        for source in original_sources:
            stream = await self.evidence.open_version(
                self.actor,
                EvidenceVersionRef(source.evidence_id, source.version),
                "daily-update-comparison",
            )
            chunks = bytearray()
            try:
                async for chunk in stream.stream:
                    chunks.extend(chunk)
                    if len(chunks) > MAX_EVIDENCE_BYTES:
                        raise ValueError("ORIGINAL_CONTEXT_LIMIT")
            finally:
                closer = getattr(stream.stream, "aclose", None)
                if closer is not None:
                    await closer()
            data = bytes(chunks)
            if hashlib.sha256(data).hexdigest() != source.sha256:
                raise ValueError("ORIGINAL_VERSION_MISMATCH")
            # Revalidate access after I/O; a revoked original must not enter model context.
            check = await self.evidence.open_version(
                self.actor,
                EvidenceVersionRef(source.evidence_id, source.version),
                "daily-update-comparison-recheck",
            )
            closer = getattr(check.stream, "aclose", None)
            if closer is not None:
                await closer()
            images.append(
                ModelImagePart(
                    mime_type=cast(Literal["image/jpeg", "image/png"], source.mime_type),
                    data=data,
                    source_ref=f"{source.evidence_id}:v{source.version}",
                )
            )
        scope = replace(
            current, evidence_versions=tuple((s.evidence_id, s.version) for s in original_sources)
        )
        with daily_model_scope(scope):
            response = await self.gateway.generate_structured(
                StructuredModelRequest(
                    invocation_key="daily_update.compare",
                    messages=(
                        ModelMessage(
                            role="system",
                            content=COMPARE_ORIGINALS_V2,
                        ),
                        ModelMessage(
                            role="user",
                            content=json.dumps(
                                {
                                    "claims": [c.model_dump(mode="json") for c in resolved],
                                    "sources": [
                                        s.model_dump(mode="json") for s in original_sources
                                    ],
                                }
                            ),
                            images=tuple(images),
                        ),
                    ),
                    output_schema=ComparisonOutput,
                    timeout_seconds=min(60, budget.timeout_seconds),
                    max_output_tokens=1500,
                )
            )
        findings = response.parsed.findings
        judgment = EvidenceJudgment.model_validate(response.parsed.model_dump(exclude={"findings"}))
        evaluate_support(
            tuple(resolved),
            findings,
            SourceCoverage(processed_count=len(images), total_count=len(original_sources)),
            judgment,
        )
        return EvidenceComparisonResult(
            judgment=judgment,
            provenance=AssessmentProvenance(
                prompt_versions=("daily-update.claims.v1", "daily-update.compare.v2"),
                model_refs=(inventory.model_ref, response.model_ref),
            ),
            claims=tuple(resolved),
            findings=findings,
            processed_sources=tuple(
                SelectedEvidence(evidence_id=s.evidence_id, version=s.version)
                for s in original_sources
            ),
        )
