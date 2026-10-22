"""Real budget controller refuses before external I/O; storage is an injected boundary."""

import pytest

from work_management_ai.agents.retrieve.contracts import RetrieveLimits
from work_management_ai.agents.retrieve.tests.fixtures import Usage, handoff


@pytest.mark.parametrize(
    ("first", "second"), [((8000, 100), (5000, 100)), ((6000, 1600), (6001, 1600))]
)
@pytest.mark.asyncio
async def test_cumulative_reservations_stop_before_the_second_provider_call(
    first: tuple[int, int],
    second: tuple[int, int],
) -> None:
    from work_management_ai.agents.retrieve.budget import RetrievalBudget, RetrievalBudgetError

    budget = await RetrievalBudget.start(
        handoff=handoff(), limits=RetrieveLimits(), usage_port=Usage()
    )
    await budget.reserve_model(input_tokens=first[0], output_tokens=first[1])
    with pytest.raises(RetrievalBudgetError):
        await budget.reserve_model(input_tokens=second[0], output_tokens=second[1])


@pytest.mark.asyncio
async def test_third_model_attempt_and_oversized_single_output_are_refused() -> None:
    from work_management_ai.agents.retrieve.budget import RetrievalBudget, RetrievalBudgetError

    budget = await RetrievalBudget.start(
        handoff=handoff(), limits=RetrieveLimits(), usage_port=Usage()
    )
    with pytest.raises(RetrievalBudgetError):
        await budget.reserve_model(input_tokens=100, output_tokens=1601)
    await budget.reserve_model(input_tokens=100, output_tokens=1600)
    await budget.reserve_model(input_tokens=100, output_tokens=1600)
    with pytest.raises(RetrievalBudgetError):
        await budget.reserve_model(input_tokens=100, output_tokens=1)


@pytest.mark.asyncio
async def test_effective_limits_and_expiry_do_not_reset_across_resume() -> None:
    from work_management_ai.agents.retrieve.budget import RetrievalBudget, RetrievalBudgetError

    request, usage = handoff(), Usage()
    limits = RetrieveLimits(max_model_attempts=1, max_output_tokens_per_attempt=100)
    budget = await RetrievalBudget.start(handoff=request, limits=limits, usage_port=usage)
    await budget.reserve_model(input_tokens=100, output_tokens=100)
    resumed = await RetrievalBudget.start(handoff=request, limits=limits, usage_port=usage)
    with pytest.raises(RetrievalBudgetError):
        await resumed.reserve_model(input_tokens=100, output_tokens=1)
    usage.elapsed = 26
    with pytest.raises(RetrievalBudgetError):
        await RetrievalBudget.start(handoff=request, limits=limits, usage_port=usage)


@pytest.mark.asyncio
async def test_node_budget_and_retrieval_deadline_are_shared() -> None:
    from work_management_ai.agents.retrieve.budget import RetrievalBudget, RetrievalBudgetError

    usage = Usage()
    budget = await RetrievalBudget.start(
        handoff=handoff(), limits=RetrieveLimits(), usage_port=usage
    )
    for i in range(12):
        await budget.enter_node(str(i))
    with pytest.raises(RetrievalBudgetError):
        await budget.enter_node("thirteenth")
    usage.elapsed = 9
    assert await budget.remaining_seconds(retrieval=True) == 0
    assert 0 < await budget.remaining_seconds() < 17
