"""Exercise the real original adapter and support policy with synthetic model output."""

import argparse
import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.progress.adapters.daily_update_comparison import OriginalEvidenceComparison
from app.modules.progress.application.assessment_service import ComparisonBudget, OriginalSource
from app.modules.progress.domain.daily_updates import DailyUpdateError, SelectedEvidence
from app.modules.progress.domain.evidence import (
    AuthorizedEvidenceStream,
    EvidenceOriginal,
    EvidenceVersionRef,
    StoredBlob,
)
from app.modules.progress.domain.evidence_support import Claim, SourceCoverage, evaluate_support
from work_management_ai.evaluation.phase4_cases import EvidenceCase, load_fixtures
from work_management_ai.model_gateway.errors import ModelGatewayError, ModelTimeoutError
from work_management_ai.model_gateway.mock import MockModelGateway


class Originals:
    def __init__(self, data: bytes, mime: str):
        self.data, self.mime = data, mime

    async def open_version(
        self, actor: AuthenticatedActor, ref: EvidenceVersionRef, request_id: str
    ) -> AuthorizedEvidenceStream:
        async def stream() -> AsyncIterator[bytes]:
            yield self.data

        return AuthorizedEvidenceStream(
            EvidenceOriginal(
                ref,
                "synthetic",
                StoredBlob(
                    "synthetic-private", sha256(self.data).hexdigest(), len(self.data), self.mime
                ),
                datetime.now(UTC),
                None,
            ),
            stream(),
        )


async def evaluate(case: EvidenceCase) -> dict[str, int]:
    metrics = dict(
        total=1,
        passed=0,
        explicit_contradiction_cases=int(case.finding == "CONTRADICTED"),
        explicit_contradictions_warned=0,
        invalid_delivered_source_refs=0,
        fabricated_unassessable_scores=0,
        score_preservation_failures=0,
        warning_true_positives=0,
        warning_false_positives=0,
        warning_false_negatives=0,
        coverage_correct=0,
    )
    actor = AuthenticatedActor(
        UUID(int=1),
        "synthetic@example.test",
        "Synthetic",
        UUID(int=2),
        UUID(int=3),
        "Synthetic",
        MembershipRole.EMPLOYEE,
    )
    ref = SelectedEvidence(evidence_id=UUID(int=4), version=1)
    mime = {
        "PNG": "image/png",
        "JPEG": "image/jpeg",
        "PDF": "application/pdf",
        "DOCX": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "MARKDOWN": "text/markdown",
        "TEXT": "text/plain",
    }[case.source_format]
    data = (
        b"\xff\xd8\xff" if mime == "image/jpeg" else b"\x89PNG\r\n\x1a\n"
    ) + b"synthetic fixture"
    claim = Claim(
        id="line:0",
        source_span="items[0].done_text",
        text=case.claim,
        task_id=UUID(int=5),
        evidence_refs=(ref,),
    )
    cited = ref.model_dump(mode="json")
    if case.source_reference == "FOREIGN":
        cited["evidence_id"] = str(UUID(int=404))
    if case.source_reference == "WRONG_VERSION":
        cited["version"] = 2
    fixture: object = {
        "score": case.model_score,
        "rationale": "Synthetic model assessment",
        "recommendations": [],
        "findings": [
            {
                "claim_id": "c0",
                "finding": case.finding,
                "source_refs": [] if case.finding == "UNASSESSABLE" else [cited],
                "limitation": "Unreadable synthetic evidence"
                if case.finding == "UNASSESSABLE"
                else "",
            }
        ],
    }
    if case.scenario == "provider-timeout":
        fixture = ModelTimeoutError("synthetic timeout")
    adapter = OriginalEvidenceComparison(
        gateway=MockModelGateway(
            fixtures={
                "daily_update.claims": {
                    "claims": [
                        {
                            "source_claim_id": claim.id,
                            "text": "Completed" if case.inventory == "OMITTED" else claim.text,
                            "category": "WORK",
                            "checkability": True,
                        }
                    ]
                },
                "daily_update.compare": fixture,
            }
        ),
        evidence=Originals(data, mime),
        actor=actor,
        run_id=UUID(int=6),
    )
    try:
        comparison = await adapter.compare(
            (claim,),
            (
                OriginalSource(
                    evidence_id=ref.evidence_id,
                    version=1,
                    sha256=sha256(data).hexdigest(),
                    mime_type=mime,
                ),
            ),
            ComparisonBudget(),
        )
        assert comparison.claims is not None
        result = evaluate_support(
            comparison.claims,
            comparison.findings,
            SourceCoverage(processed_count=case.processed_count, total_count=case.total_count),
            comparison.judgment,
        )
    except (ValueError, ValidationError, DailyUpdateError, ModelGatewayError) as exc:
        code = (
            "MODEL_TIMEOUT"
            if isinstance(exc, ModelTimeoutError)
            else str(getattr(exc, "code", str(exc)))
        )
        metrics["passed"] = int(case.expect_rejection and code == case.expected_error)
        return metrics
    actual, expected = set(result.warning_codes), set(case.expected_warnings)
    metrics["warning_true_positives"] = len(actual & expected)
    metrics["warning_false_positives"] = len(actual - expected)
    metrics["warning_false_negatives"] = len(expected - actual)
    metrics["explicit_contradictions_warned"] = int(
        case.finding == "CONTRADICTED" and "CONTRADICTION" in actual
    )
    metrics["fabricated_unassessable_scores"] = int(
        case.finding == "UNASSESSABLE" and result.score is not None
    )
    metrics["invalid_delivered_source_refs"] = sum(
        not set(f.source_refs) <= {ref} for f in comparison.findings
    )
    metrics["score_preservation_failures"] = int(
        (str(result.score) if result.score is not None else None) != case.expected_score
    )
    metrics["coverage_correct"] = (
        int((case.processed_count < case.total_count) == ("INSUFFICIENT_ASSESSMENT" in actual))
        if case.finding != "UNASSESSABLE"
        else int("INSUFFICIENT_ASSESSMENT" in actual)
    )
    metrics["passed"] = int(
        not case.expect_rejection
        and actual == expected
        and not metrics["score_preservation_failures"]
        and not metrics["invalid_delivered_source_refs"]
        and not metrics["fabricated_unassessable_scores"]
        and metrics["coverage_correct"] == 1
    )
    return metrics


async def run(root: Path) -> dict[str, int]:
    results = [await evaluate(case) for fixture in load_fixtures(root) for case in fixture.evidence]
    return {key: sum(result[key] for result in results) for key in results[0]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, required=True)
    print(json.dumps(asyncio.run(run(parser.parse_args().fixtures)), sort_keys=True))
