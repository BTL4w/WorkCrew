"""Repository closure gate over real harnesses/adapters and synthetic bilingual fixtures."""

import asyncio
import json
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from work_management_ai.evaluation.phase4_cases import FIXTURE_ROOT, load_fixtures
from work_management_ai.evaluation.phase4_daily_update import evaluate as evaluate_daily
from work_management_ai.evaluation.phase4_risk import evaluate as evaluate_risk


class EvaluationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    total: int
    passed: int
    daily_update_cases: int
    risk_cases: int
    evidence_cases: int
    policy_violations: int
    explicit_contradiction_cases: int
    explicit_contradictions_warned: int
    invalid_delivered_source_refs: int
    fabricated_unassessable_scores: int
    score_preservation_failures: int
    warning_precision: float
    warning_recall: float
    coverage_correct: int
    locales: tuple[str, ...]
    source_formats: tuple[str, ...]
    provider: str = "deterministic_mock"
    hosted_model_quality_measured: bool = False


async def _agents(root: Path) -> tuple[int, int, int, int, int]:
    daily = [
        await evaluate_daily(case)
        for fixture in load_fixtures(root)
        for case in fixture.daily_updates
    ]
    risk: list[bool] = []
    risk_metrics: list[dict[str, int]] = []
    for fixture in load_fixtures(root):
        for case in fixture.risks:
            metrics: dict[str, int] = {}
            risk.append(await evaluate_risk(case, metrics))
            risk_metrics.append(metrics)
    violations = sum(bypass + peers + leaks for _, bypass, peers, leaks in daily)
    violations += sum(m["policy_violations"] for m in risk_metrics)
    return (
        len(daily),
        len(risk),
        sum(passed for passed, _, _, _ in daily) + sum(risk),
        violations,
        sum(m["invalid_delivered_source_refs"] for m in risk_metrics),
    )


def run_phase4_evaluation(root: Path = FIXTURE_ROOT) -> EvaluationSummary:
    fixtures = load_fixtures(root)
    daily_count, risk_count, passed, violations, risk_invalid_sources = asyncio.run(_agents(root))
    repository = Path(__file__).resolve().parents[4]
    process = subprocess.run(
        [
            "uv",
            "run",
            "--directory",
            str(repository / "backend"),
            "python",
            "-m",
            "app.scripts.evaluate_phase4_evidence",
            "--fixtures",
            str(root.resolve()),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=90,
    )
    metrics: dict[str, int] = json.loads(process.stdout)
    tp, fp, fn = (
        metrics[key]
        for key in ("warning_true_positives", "warning_false_positives", "warning_false_negatives")
    )
    return EvaluationSummary(
        total=daily_count + risk_count + metrics["total"],
        passed=passed + metrics["passed"],
        daily_update_cases=daily_count,
        risk_cases=risk_count,
        evidence_cases=metrics["total"],
        policy_violations=violations,
        explicit_contradiction_cases=metrics["explicit_contradiction_cases"],
        explicit_contradictions_warned=metrics["explicit_contradictions_warned"],
        invalid_delivered_source_refs=metrics["invalid_delivered_source_refs"]
        + risk_invalid_sources,
        fabricated_unassessable_scores=metrics["fabricated_unassessable_scores"],
        score_preservation_failures=metrics["score_preservation_failures"],
        warning_precision=tp / (tp + fp) if tp + fp else 1,
        warning_recall=tp / (tp + fn) if tp + fn else 1,
        coverage_correct=metrics["coverage_correct"],
        locales=tuple(sorted({fixture.locale for fixture in fixtures})),
        source_formats=tuple(
            sorted({case.source_format for fixture in fixtures for case in fixture.evidence})
        ),
    )


def gate(summary: EvaluationSummary) -> bool:
    return (
        summary.total > 0
        and summary.passed == summary.total
        and summary.policy_violations == 0
        and summary.explicit_contradiction_cases > 0
        and summary.explicit_contradictions_warned == summary.explicit_contradiction_cases
        and summary.invalid_delivered_source_refs == 0
        and summary.fabricated_unassessable_scores == 0
        and summary.score_preservation_failures == 0
        and summary.warning_precision == summary.warning_recall == 1
    )


if __name__ == "__main__":
    summary = run_phase4_evaluation()
    print(summary.model_dump_json())
    raise SystemExit(0 if gate(summary) else 1)
