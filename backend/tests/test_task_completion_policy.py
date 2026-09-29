from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.modules.progress.domain.completion import (
    CompletionCriterion,
    CompletionObservation,
    CompletionRequirementError,
    CriterionAttestation,
    validate_completion,
)
from app.modules.progress.domain.daily_updates import SelectedEvidence
from app.modules.work.domain.tasks import TaskStatus


def test_employee_done_requires_evidence_backed_report() -> None:
    with pytest.raises(CompletionRequirementError, match="COMPLETION_REPORT_REQUIRED"):
        validate_completion("EMPLOYEE", TaskStatus.DONE, None, (), (), None)
    evidence = SelectedEvidence(evidence_id=uuid4(), version=1)
    observation = CompletionObservation(uuid4(), Decimal(100), (evidence,), datetime.now(UTC))
    assert validate_completion(
        "EMPLOYEE",
        TaskStatus.DONE,
        observation,
        (),
        (),
        observation.confirmed_at - timedelta(seconds=1),
    ).allowed
    with pytest.raises(CompletionRequirementError, match="COMPLETION_REPORT_REQUIRED"):
        validate_completion(
            "EMPLOYEE",
            TaskStatus.DONE,
            observation,
            (),
            (),
            observation.confirmed_at + timedelta(seconds=1),
        )


def test_current_criteria_require_exact_attestations() -> None:
    criterion = CompletionCriterion(uuid4(), 2)
    observation = CompletionObservation(
        uuid4(),
        Decimal(100),
        (SelectedEvidence(evidence_id=uuid4(), version=1),),
        datetime.now(UTC),
    )
    with pytest.raises(CompletionRequirementError, match="COMPLETION_CRITERIA_REQUIRED"):
        validate_completion("EMPLOYEE", TaskStatus.DONE, observation, (criterion,), (), None)
    with pytest.raises(CompletionRequirementError, match="COMPLETION_CRITERIA_STALE"):
        validate_completion(
            "EMPLOYEE",
            TaskStatus.DONE,
            observation,
            (criterion,),
            (CriterionAttestation(criterion.id, 1, True, ()),),
            None,
        )
    assert validate_completion(
        "EMPLOYEE",
        TaskStatus.DONE,
        observation,
        (criterion,),
        (CriterionAttestation(criterion.id, 2, True, ()),),
        None,
    ).allowed


def test_manager_manual_completion_skips_report_but_not_criteria() -> None:
    assert validate_completion("MANAGER", TaskStatus.DONE, None, (), (), None).allowed
    criterion = CompletionCriterion(uuid4(), 1)
    with pytest.raises(CompletionRequirementError, match="COMPLETION_CRITERIA_REQUIRED"):
        validate_completion("MANAGER", TaskStatus.DONE, None, (criterion,), (), None)
