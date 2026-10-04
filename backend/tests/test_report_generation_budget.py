"""Whole-run reporting reservations survive local harness reconstruction."""

from uuid import uuid4

import pytest

from work_management_ai.agents.reporting.contracts import ReportingUsageScope


def test_reporting_usage_scope_binds_generation_and_fence():
    scope = ReportingUsageScope(
        organization_id=uuid4(),
        membership_id=uuid4(),
        generation_id=uuid4(),
        fence=1,
        worker_id="worker",
    )
    assert scope.generation_id
    with pytest.raises(ValueError):
        ReportingUsageScope.model_validate({**scope.model_dump(), "fence": 0})


def test_model_output_limits_are_generation_or_grounding_specific():
    from work_management_ai.agents.reporting.contracts import ReportingUsage

    usage = ReportingUsage(
        attempts=3, input_reserved=48000, output_reserved=8000, tools=6, retries=1
    )
    assert usage.attempts == 3
    for field, value in (
        ("attempts", 4),
        ("input_reserved", 48001),
        ("output_reserved", 8001),
        ("tools", 7),
        ("retries", 2),
    ):
        with pytest.raises(ValueError):
            ReportingUsage.model_validate({**usage.model_dump(), field: value})
