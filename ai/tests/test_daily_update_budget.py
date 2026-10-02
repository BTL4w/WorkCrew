from uuid import uuid4

import pytest

from work_management_ai.runtime.daily_update_budget import (
    BudgetScope,
    MemoryUsageStore,
    UsageLimitExceeded,
)


@pytest.mark.asyncio
async def test_checkpoint_retry_does_not_reset_model_attempts_or_evidence_budget():
    store = MemoryUsageStore()
    scope = BudgetScope(
        organization_id=uuid4(),
        membership_id=uuid4(),
        run_id=uuid4(),
        evidence_versions=((uuid4(), 1),),
    )
    for _ in range(3):
        await store.reserve(scope, input_tokens=1000, output_tokens=1000)
    with pytest.raises(UsageLimitExceeded):
        await store.reserve(scope, input_tokens=1000, output_tokens=1000)
    recovered = BudgetScope(**scope.__dict__)
    with pytest.raises(UsageLimitExceeded):
        await store.reserve(recovered, input_tokens=1, output_tokens=1)


@pytest.mark.asyncio
async def test_tokens_are_reserved_before_attempt_even_on_failure():
    store = MemoryUsageStore()
    scope = BudgetScope(organization_id=uuid4(), membership_id=uuid4(), run_id=uuid4())
    await store.reserve(scope, input_tokens=24000, output_tokens=4000)
    with pytest.raises(UsageLimitExceeded):
        await store.reserve(scope, input_tokens=1, output_tokens=1)


@pytest.mark.asyncio
async def test_new_run_does_not_reset_per_version_read_budget():
    store = MemoryUsageStore()
    org, member, evidence = uuid4(), uuid4(), uuid4()
    for _ in range(10):
        await store.reserve(
            BudgetScope(
                organization_id=org,
                membership_id=member,
                run_id=uuid4(),
                evidence_versions=((evidence, 1),),
            ),
            input_tokens=100,
            output_tokens=100,
        )
    with pytest.raises(UsageLimitExceeded):
        await store.reserve(
            BudgetScope(
                organization_id=org,
                membership_id=member,
                run_id=uuid4(),
                evidence_versions=((evidence, 1),),
            ),
            input_tokens=100,
            output_tokens=100,
        )


@pytest.mark.asyncio
async def test_transient_retry_charges_two_attempts_before_success():
    from pydantic import BaseModel

    from work_management_ai.model_gateway.contracts import (
        ModelMessage,
        StructuredModelRequest,
        StructuredModelResponse,
    )
    from work_management_ai.model_gateway.errors import ModelTimeoutError
    from work_management_ai.runtime.budgeted_gateway import BudgetedDailyGateway
    from work_management_ai.runtime.daily_update_budget import daily_model_scope

    class Output(BaseModel):
        value: str

    class Gateway:
        calls: int = 0

        async def generate_structured[OutputT: BaseModel](
            self, request: StructuredModelRequest[OutputT]
        ) -> StructuredModelResponse[OutputT]:
            self.calls += 1
            if self.calls == 1:
                raise ModelTimeoutError("temporary timeout")
            return StructuredModelResponse(
                parsed=request.output_schema.model_validate({"value": "ok"}), model_ref="mock:daily"
            )

    store = MemoryUsageStore()
    scope = BudgetScope(organization_id=uuid4(), membership_id=uuid4(), run_id=uuid4())
    provider = Gateway()
    with daily_model_scope(scope):
        response = await BudgetedDailyGateway(provider, store).generate_structured(
            StructuredModelRequest(
                invocation_key="test",
                messages=(ModelMessage(role="user", content="hello"),),
                output_schema=Output,
                timeout_seconds=60,
                max_output_tokens=100,
            )
        )
    assert response.parsed.value == "ok"
    assert next(iter(store.counts.values()))[0] == 2


@pytest.mark.asyncio
async def test_recovery_keeps_original_wall_clock_deadline():
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    store = MemoryUsageStore()
    scope = BudgetScope(
        organization_id=uuid4(),
        membership_id=uuid4(),
        run_id=uuid4(),
        started_at=datetime.now(UTC) - timedelta(seconds=179),
    )
    await store.reserve(scope, input_tokens=100, output_tokens=100)
    remaining = await store.remaining_seconds(replace(scope, started_at=datetime.now(UTC)))
    assert 0 < remaining <= 1
    store.starts[(scope.organization_id, scope.membership_id, scope.run_id)] -= timedelta(seconds=2)
    with pytest.raises(UsageLimitExceeded):
        await store.reserve(scope, input_tokens=100, output_tokens=100)
