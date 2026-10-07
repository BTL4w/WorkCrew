"""Server-owned provider policy and typed measurement validation; never persist raw model data."""

import re
from collections.abc import Awaitable, Callable
from typing import cast

from pydantic import JsonValue

from app.core.config import Settings
from app.modules.reporting.domain.reports import ReportError
from app.modules.reporting.domain.snapshots import canonical_hash
from work_management_ai.evaluation.phase5_reporting import (
    REQUIRED_COVERAGE,
    EvaluationGateResult,
    ReportingCaseResult,
    evaluate_gate,
)

from ..domain.evaluation import EvaluationDatasetVersion, ReportEvaluationResult
from ..domain.evaluation_runs import PROVIDER_POLICY, EvaluationRun
from .evaluation_runner import (
    HostedReportingEvaluationGateway,
    ReportingEvaluationProvider,
    hosted_policy,
)


class EvaluationPolicy:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.budget = settings.report_evaluation_budget_tokens

    def fingerprint(self, provider: str) -> str:
        return canonical_hash(
            {
                "policy": PROVIDER_POLICY,
                "provider": provider,
                "budget": self.budget,
                "model": self.settings.ai_model
                if provider == "hosted"
                else "mock:reporting-eval.v1",
                "judge": False,
                "hosted_enabled": self.settings.report_evaluation_hosted_enabled
                if provider == "hosted"
                else False,
            }
        )

    def validate(self, provider: str) -> None:
        if provider == "hosted":
            hosted_policy(self.settings, budget_tokens=self.budget)

    def build(
        self, job: EvaluationRun, authorize: Callable[[], Awaitable[None]]
    ) -> ReportingEvaluationProvider:
        if job.provider == "mock":
            return ReportingEvaluationProvider()
        return ReportingEvaluationProvider(
            HostedReportingEvaluationGateway(
                self.settings,
                budget_tokens=job.budget_tokens,
                authorize=authorize,
            )
        )

    def verify(
        self, job: EvaluationRun, dataset: EvaluationDatasetVersion, result: ReportEvaluationResult
    ) -> ReportEvaluationResult:
        return validate_measurements(
            job,
            dataset,
            result,
            model_ref=f"openai:{self.settings.ai_model}"
            if job.provider == "hosted"
            else "mock:reporting-eval.v1",
        )


def validate_measurements(
    job: EvaluationRun,
    dataset: EvaluationDatasetVersion,
    result: ReportEvaluationResult,
    *,
    model_ref: str,
) -> ReportEvaluationResult:
    if (
        result.dataset_version_id != dataset.id
        or result.dataset_hash != dataset.dataset_hash
        or result.provider != job.provider
        or result.total != len(dataset.cases)
        or len(result.cases) != len(dataset.cases)
    ):
        raise ReportError("EVALUATION_RESULT_INVALID")
    expected_limits = (
        "SYNTHETIC_SCAFFOLD",
        "MOCK_USAGE_NOT_HOSTED_TOKENS"
        if job.provider == "mock"
        else "HOSTED_QUALITATIVE_NOT_JUDGED",
        "METRIC_ONLY_DATASET_MAY_NOT_EXERCISE_ALL_REQUIRED_POLICY_DIMENSIONS",
    )
    if result.hosted_quality != "NOT_RUN" or result.limitations != expected_limits:
        raise ReportError("EVALUATION_RESULT_INVALID")
    measured = tuple(ReportingCaseResult.model_validate(value) for value in result.cases)
    for case, item in zip(dataset.cases, measured, strict=True):
        provenance = item.provenance
        trusted = {
            "dataset_version_id": str(dataset.id),
            "case_id": str(case.id),
            "case_version": case.version,
            "case_hash": case.case_hash,
            "split": case.split,
            "origin": case.origin,
            "policy_version": case.policy_version,
        }
        version_literals = {
            "workflow": "reporting-narrative.v1",
            "prompt": "reporting.system.v1",
            "grounding_prompt": "reporting.grounding.v1",
            "suite": "reporting-eval.v1",
        }
        semver_keys = {"agent", "numeric_verifier", "semantic_verifier"}
        if (
            any(provenance.get(key) != value for key, value in trusted.items())
            or item.locale != case.payload.locale
            or item.expected != "PROPOSAL"
            or item.actual_status not in ("AWAITING_HUMAN", "FAILED")
            or item.judge_status != "NOT_RUN"
            or item.qualitative_scores is not None
            or any(ref != model_ref for ref in item.model_refs)
            or set(item.versions) != set(version_literals) | semver_keys | {"manifest"}
            or any(item.versions.get(key) != value for key, value in version_literals.items())
            or any(
                not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", item.versions.get(key, ""))
                for key in semver_keys
            )
            or not re.fullmatch(r"[a-f0-9]{64}", item.versions.get("manifest", ""))
            or not item.coverage.issubset(REQUIRED_COVERAGE)
            or not 0 <= item.usage_missing_calls <= item.model_calls
            or not 0 <= item.model_calls <= 3
            or not 0 <= item.latency_ms <= 300000
            or any(
                value is not None and value < 0 for value in (item.input_tokens, item.output_tokens)
            )
            or any(
                not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code)
                for code in (*item.safe_codes, *item.observed_evidence)
            )
        ):
            raise ReportError("EVALUATION_RESULT_INVALID")
        if (
            item.id != f"{case.id}:v{case.version}"
            or provenance.get("case_id") != str(case.id)
            or provenance.get("case_version") != case.version
            or provenance.get("case_hash") != case.case_hash
        ):
            raise ReportError("EVALUATION_RESULT_INVALID")
    gate = evaluate_gate(measured)
    if (
        EvaluationGateResult.model_validate(result.gate) != gate
        or result.gate_passed != gate.passed
        or result.passed != sum(item.passed and not item.skipped for item in measured)
        or result.failed != sum(not item.passed and not item.skipped for item in measured)
        or result.skipped != sum(item.skipped for item in measured)
    ):
        raise ReportError("EVALUATION_RESULT_INVALID")
    # Keep case identity/hash/split, not nested source context copies.
    safe = tuple(
        {
            **item.model_dump(mode="json", exclude={"provenance"}),
            "provenance": {
                "dataset_version_id": str(dataset.id),
                "case_id": str(case.id),
                "case_version": case.version,
                "case_hash": case.case_hash,
                "split": case.split,
                "origin": case.origin,
                "policy_version": case.policy_version,
            },
        }
        for case, item in zip(dataset.cases, measured, strict=True)
    )
    return result.model_copy(
        update={"cases": safe, "gate": cast(dict[str, JsonValue], gate.model_dump(mode="json"))}
    )
