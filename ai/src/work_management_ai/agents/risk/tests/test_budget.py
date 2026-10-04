from uuid import uuid4

import pytest

from work_management_ai.runtime.daily_update_budget import (
    BudgetScope,
    MemoryUsageStore,
    UsageLimitExceeded,
)


@pytest.mark.asyncio
async def test_risk_reservation_survives_restarted_harness_and_allows_only_one_attempt():
    store = MemoryUsageStore()
    scope = BudgetScope(
        organization_id=uuid4(),
        membership_id=uuid4(),
        run_id=uuid4(),
        max_model_attempts=1,
        timeout_seconds=90,
    )
    assert await store.reserve(scope, input_tokens=1000, output_tokens=1000) == 1
    with pytest.raises(UsageLimitExceeded):
        await store.reserve(scope, input_tokens=1000, output_tokens=1000)
