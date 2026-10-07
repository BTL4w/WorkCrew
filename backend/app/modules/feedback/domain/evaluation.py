"""Minimal human-reviewed numeric fixtures, exact provenance and immutable dataset manifests."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from app.modules.reporting.domain.metrics import MetricState, MetricValue, ReportContract
from app.modules.reporting.domain.snapshots import canonical_hash

DATASET_POLICY = "report-eval-dataset.v1"


class EvaluationPayload(ReportContract):
    schema_version: Literal["report-eval-case.v1"] = "report-eval-case.v1"
    locale: Literal["vi", "en"]
    period_kind: Literal["DAILY", "WEEKLY"]
    metrics: dict[str, MetricValue] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def minimal_context(self) -> Self:
        for key, metric in self.metrics.items():
            if (
                key != metric.key
                or metric.source_refs
                or metric.limitations
                or metric.policy_version != "report-metrics.v1"
            ):
                raise ValueError("UNSAFE_EVALUATION_CONTEXT")
        return self


class EvaluationAssertion(ReportContract):
    metric_key: str
    value: Decimal | None
    unit: Literal["COUNT", "HOURS", "FRACTION", "PERCENT", "SCORE", "DAYS"]
    state: MetricState
    time_basis: Literal["AT_CAPTURE", "IN_PERIOD", "DECLARED_REPORTING_DATE"]


class CurateCaseCommand(ReportContract):
    candidate_id: UUID
    payload: EvaluationPayload
    expected_assertions: tuple[EvaluationAssertion, ...] = Field(min_length=1, max_length=100)
    origin: Literal["SYNTHETIC", "REDACTED"]
    split: Literal["GOLDEN", "HELD_OUT"]
    human_review_decision: Literal["APPROVE", "REJECT"]
    permission_reviewed: bool

    def validate_review(self) -> None:
        if self.human_review_decision != "APPROVE" or not self.permission_reviewed:
            raise ValueError("EVALUATION_REVIEW_REQUIRED")
        self.validate_assertions()

    def validate_assertions(self) -> None:
        seen: set[str] = set()
        for assertion in self.expected_assertions:
            metric = self.payload.metrics.get(assertion.metric_key)
            if (
                metric is None
                or assertion.metric_key in seen
                or assertion.value != metric.value
                or assertion.unit != metric.unit
                or assertion.state != metric.state
                or assertion.time_basis != metric.time_basis
            ):
                raise ValueError("EVALUATION_ASSERTION_MISMATCH")
            seen.add(assertion.metric_key)
        if seen != set(self.payload.metrics):
            raise ValueError("EVALUATION_ASSERTION_MISMATCH")


def normalized_case_hash(
    payload: EvaluationPayload, assertions: tuple[EvaluationAssertion, ...]
) -> str:
    def number(value: Decimal | None):
        if value is None:
            return None
        if value == 0:
            return "0"
        exact = format(value, "f")
        return exact.rstrip("0").rstrip(".") if "." in exact else exact

    data = payload.model_dump(mode="json")
    for key, metric in payload.metrics.items():
        data["metrics"][key]["value"] = number(metric.value)
    expected: list[dict[str, Any]] = []
    for assertion in sorted(assertions, key=lambda item: item.metric_key):
        item = assertion.model_dump(mode="json")
        item["value"] = number(assertion.value)
        expected.append(item)
    return canonical_hash({"policy": DATASET_POLICY, "payload": data, "assertions": expected})


class EvaluationCandidate(ReportContract):
    id: UUID
    feedback_id: UUID
    generation_id: UUID
    original_version_id: UUID
    report_version_id: UUID
    snapshot_id: UUID
    snapshot_hash: str
    version: int = Field(ge=1)
    status: Literal["PENDING_REVIEW", "CURATED", "DUPLICATE"]
    payload_classification: Literal["RAW_CANDIDATE"] = "RAW_CANDIDATE"
    payload: EvaluationPayload | None
    outcome_ids: tuple[UUID, ...] = ()
    provenance: dict[str, JsonValue]
    created_at: datetime
    expires_at: datetime


class EvaluationCase(ReportContract):
    id: UUID
    revision_id: UUID
    candidate_id: UUID
    feedback_id: UUID
    version: int = Field(ge=1)
    origin: Literal["SYNTHETIC", "REDACTED"]
    split: Literal["GOLDEN", "HELD_OUT"]
    policy_version: Literal["report-eval-dataset.v1"] = "report-eval-dataset.v1"
    case_hash: str
    payload: EvaluationPayload
    expected_assertions: tuple[EvaluationAssertion, ...]
    curator_membership_id: UUID
    human_review_decision: Literal["APPROVE"] = "APPROVE"
    permission_reviewed: Literal[True] = True
    provenance: dict[str, JsonValue]
    created_at: datetime


class DatasetCaseSelection(ReportContract):
    case_id: UUID
    version: int = Field(ge=1)


class DatasetCommand(ReportContract):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    split: Literal["GOLDEN", "HELD_OUT"]
    cases: tuple[DatasetCaseSelection, ...] = Field(default=(), max_length=100)
    include_redacted: bool = False


class EvaluationDatasetVersion(ReportContract):
    id: UUID
    name: str
    version: int
    split: Literal["GOLDEN", "HELD_OUT"]
    policy_version: Literal["report-eval-dataset.v1"] = "report-eval-dataset.v1"
    dataset_hash: str
    cases: tuple[EvaluationCase, ...]
    frozen_by_membership_id: UUID
    created_at: datetime

    def verified_hash(self) -> bool:
        return self.dataset_hash == dataset_hash(self.name, self.split, self.cases)


def dataset_hash(name: str, split: str, cases: tuple[EvaluationCase, ...]) -> str:
    members = sorted(
        (
            {
                "case_id": str(case.id),
                "version": case.version,
                "case_hash": case.case_hash,
                "origin": case.origin,
                "split": case.split,
                "provenance": case.provenance,
            }
            for case in cases
        ),
        key=lambda item: (item["case_id"], item["version"]),
    )
    return canonical_hash(
        {"name": name, "split": split, "policy": DATASET_POLICY, "cases": members}
    )


class EvaluationReviewDiff(ReportContract):
    candidate_id: UUID
    candidate_version: int
    before: EvaluationPayload
    after: EvaluationPayload
    expected_assertions: tuple[EvaluationAssertion, ...]
    origin: Literal["SYNTHETIC", "REDACTED"]
    split: Literal["GOLDEN", "HELD_OUT"]
    case_hash: str


class ReportEvaluationResult(ReportContract):
    dataset_version_id: UUID
    dataset_hash: str
    provider: Literal["mock", "hosted"]
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)
    gate_passed: bool
    gate: dict[str, JsonValue]
    cases: tuple[dict[str, JsonValue], ...]
    hosted_quality: str
    limitations: tuple[str, ...]
