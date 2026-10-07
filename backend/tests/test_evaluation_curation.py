from decimal import Decimal
from typing import Literal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.feedback.domain.evaluation import (
    CurateCaseCommand,
    EvaluationAssertion,
    EvaluationPayload,
    normalized_case_hash,
)
from app.modules.reporting.domain.metrics import MetricState


def payload(locale: Literal["vi", "en"] = "en", value: str = "2") -> EvaluationPayload:
    return EvaluationPayload.model_validate(
        {
            "locale": locale,
            "period_kind": "DAILY",
            "metrics": {
                "tasks.status.total_count": {
                    "key": "tasks.status.total_count",
                    "value": value,
                    "unit": "COUNT",
                    "state": "KNOWN",
                    "time_basis": "AT_CAPTURE",
                }
            },
        }
    )


def assertion(value: str = "2") -> EvaluationAssertion:
    return EvaluationAssertion(
        metric_key="tasks.status.total_count",
        value=Decimal(value),
        unit="COUNT",
        state=MetricState.KNOWN,
        time_basis="AT_CAPTURE",
    )


def test_redaction_requires_human_review():
    command = CurateCaseCommand(
        candidate_id=uuid4(),
        payload=payload(),
        expected_assertions=(assertion(),),
        origin="SYNTHETIC",
        split="GOLDEN",
        human_review_decision="APPROVE",
        permission_reviewed=False,
    )
    with pytest.raises(ValueError, match="REVIEW_REQUIRED"):
        command.validate_review()
    raw = payload().model_dump(mode="json")
    raw["metrics"]["tasks.status.total_count"]["source_refs"] = [
        {
            "resource_type": "task",
            "resource_id": str(uuid4()),
            "version": 1,
            "observed_at": "2026-10-07T00:00:00Z",
            "label": "Alice alice@example.test",
            "facts": {"secret": "sk-private"},
        }
    ]
    with pytest.raises(ValidationError):
        EvaluationPayload.model_validate(raw)
    raw = payload().model_dump(mode="json")
    raw["person"] = {"name": "Alice", "email": "alice@example.test"}
    with pytest.raises(ValidationError):
        EvaluationPayload.model_validate(raw)


def test_case_dedup_normalizes_decimal_and_assertion_order():
    first = normalized_case_hash(payload(value="2.00"), (assertion("2.00"),))
    second = normalized_case_hash(payload(value="2"), (assertion("2"),))
    assert first == second
    assert first != normalized_case_hash(payload(locale="vi"), (assertion(),))


def test_expected_assertion_is_checked_against_authored_source():
    command = CurateCaseCommand(
        candidate_id=uuid4(),
        payload=payload(),
        expected_assertions=(assertion("3"),),
        origin="SYNTHETIC",
        split="GOLDEN",
        human_review_decision="APPROVE",
        permission_reviewed=True,
    )
    with pytest.raises(ValueError, match="ASSERTION_MISMATCH"):
        command.validate_review()


def test_dedup_never_rounds_large_exact_decimal_values():
    left = "12345678901234567890123456789"
    right = "12345678901234567890123456790"
    assert normalized_case_hash(payload(value=left), (assertion(left),)) != normalized_case_hash(
        payload(value=right), (assertion(right),)
    )
