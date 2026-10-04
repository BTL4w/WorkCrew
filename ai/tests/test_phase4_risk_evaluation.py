import pytest

from work_management_ai.evaluation.phase4_risk import run_suite


@pytest.mark.asyncio
async def test_bilingual_risk_golden_cases_preserve_scores_and_permissions():
    results = await run_suite()
    assert results["total"] == results["passed"] == 14
    assert results["approval_bypass_count"] == 0
    assert results["cross_tenant_leakage_count"] == 0
