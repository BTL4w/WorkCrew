"""Coverage is a gate: missing boundary evidence and omitted locales cannot pass."""

import pytest

from work_management_ai.evaluation.progressive_planning import (
    MockProgressiveEvaluationGateway,
    load_cases,
    run_progressive_planning_suite,
)


@pytest.mark.asyncio
async def test_matrix_requires_twenty_measured_observations() -> None:
    cases = load_cases()
    report = await run_progressive_planning_suite(cases, MockProgressiveEvaluationGateway())
    assert len(report.observations) == 20
    assert report.graph_passed
    assert not report.passed
    assert len([row for row in report.observations if row.status == "NOT_RUN"]) == 6
    shortened = await run_progressive_planning_suite(cases[:-1], MockProgressiveEvaluationGateway())
    assert not shortened.passed and not shortened.coverage_complete
