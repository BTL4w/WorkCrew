"""Shared Redis limits preserve SQL reservations, accepted work and safe replay."""

import os
from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.modules.optimization.adapters.redis_runtime import create_redis_runtime
from app.modules.retrieval.application.admission import RetrievalAdmissionService
from app.modules.retrieval.domain.models import RetrievalProviderError
from tests.retrieval_fixtures import RetrievalHarness, retrieval_harness
from tests.test_ai_admission import actor
from tests.test_retrieval_replay_integration import executor_parts
from work_management_ai.runtime.contracts import ActorReference, ToolExecutionRequest
from work_management_ai.tools.web.contracts import WebSearchOutput

__all__ = ["retrieval_harness"]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.redis_integration,
    pytest.mark.skipif(
        os.getenv("RUN_REDIS_INTEGRATION") != "1", reason="requires owned SQL/Redis"
    ),
]


def runtime(*, outage: bool = False):
    return create_redis_runtime(
        Settings(
            environment="test",
            retrieval_admission_enabled=True,
            redis_namespace="retrieve-" + uuid4().hex[:20],
            redis_url=SecretStr(
                "redis://workcrew@127.0.0.1:1/0" if outage else os.environ["REDIS_TEST_URL"]
            ),
        )
    )


@pytest.mark.asyncio
async def test_retrieval_31st_actor_call_denied():
    instance = runtime()
    assert instance.limiter is not None
    admission = RetrievalAdmissionService(instance.limiter, namespace=instance.namespace)
    subject = actor()
    try:
        for _ in range(30):
            await admission.require_provider_call(subject)
        with pytest.raises(RetrievalProviderError) as error:
            await admission.require_provider_call(subject)
        assert (
            error.value.safe_code == "RETRIEVAL_RATE_LIMITED" and error.value.provider_requests == 0
        )
        await admission.require_provider_call(replace(subject, organization_id=uuid4()))
    finally:
        await instance.aclose()


@pytest.mark.asyncio
async def test_retrieval_301st_org_call_denied():
    instance = runtime()
    assert instance.limiter is not None
    admission = RetrievalAdmissionService(instance.limiter, namespace=instance.namespace)
    subject = actor()
    try:
        for _ in range(300):
            await admission.require_provider_call(replace(subject, membership_id=uuid4()))
        with pytest.raises(RetrievalProviderError, match="RETRIEVAL_RATE_LIMITED"):
            await admission.require_provider_call(replace(subject, membership_id=uuid4()))
    finally:
        await instance.aclose()


def request(h: RetrievalHarness, call: str) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        agent_run_id=h.run_id,
        tool_id="web.search",
        tool_version="1.0.0",
        call_id=call,
        actor=ActorReference(
            organization_id=h.actor.organization_id, membership_id=h.actor.membership_id
        ),
        typed_input={"query": "public planning"},
        idempotency_key=f"retrieve:{call}",
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires explicit PostgreSQL"
)
async def test_outage_keeps_accepted_job_budget_and_verified_evidence_replay(
    retrieval_harness: RetrievalHarness,
):
    h = retrieval_harness
    healthy = runtime()
    down = runtime(outage=True)
    assert healthy.limiter is not None and down.limiter is not None
    try:
        accepted, _ = executor_parts(
            h, admission=RetrievalAdmissionService(healthy.limiter, namespace=healthy.namespace)
        )
        first = await accepted.execute(request(h, "accepted"))
        assert first.status == "SUCCEEDED" and first.evidence
        unavailable, _ = executor_parts(
            h, admission=RetrievalAdmissionService(down.limiter, namespace=down.namespace)
        )
        replay = await unavailable.execute(request(h, "accepted"))
        assert replay == first
        result = await unavailable.execute(request(h, "new-after-outage"))
        assert result.status == "SUCCEEDED"
        output = WebSearchOutput.model_validate(result.typed_output)
        assert not output.evidence
        assert "RETRIEVAL_ADMISSION_UNAVAILABLE" in output.limitations
        assert output.usage.provider_requests == 0
        rows = await h.sql(
            "SELECT reserved FROM retrieval_run_budgets WHERE agent_run_id=:id", {"id": h.run_id}
        )
        assert rows.scalar_one()["provider_requests"] == 2
        over_budget = await unavailable.execute(request(h, "third-paid-operation"))
        assert over_budget.status == "REJECTED"
        assert over_budget.safe_error_code is not None and "BUDGET" in over_budget.safe_error_code
        rows = await h.sql(
            "SELECT status,result_metadata FROM retrieval_attempt_reservations "
            "WHERE agent_run_id=:id AND kind='SEARCH' ORDER BY created_at",
            {"id": h.run_id},
        )
        attempts = rows.all()
        assert len(attempts) == 2 and attempts[-1].result_metadata["provider_requests"] == 0
        assert attempts[-1].status == "COMPLETED" and not attempts[-1].result_metadata["retryable"]
        rows = await h.sql("SELECT status FROM assistant_jobs WHERE id=:id", {"id": h.job_id})
        assert rows.scalar_one() == "RUNNING"
        replay = await unavailable.execute(request(h, "new-after-outage"))
        assert replay.typed_output == result.typed_output
        rows = await h.sql(
            "SELECT reserved FROM retrieval_run_budgets WHERE agent_run_id=:id", {"id": h.run_id}
        )
        assert rows.scalar_one()["provider_requests"] == 2
    finally:
        await healthy.aclose()
        await down.aclose()


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires explicit PostgreSQL"
)
async def test_disabled_capability_never_increments_real_shared_counter(
    retrieval_harness: RetrievalHarness,
):
    h = retrieval_harness
    h.enabled = False
    instance = runtime()
    assert instance.limiter is not None and instance.client is not None
    try:
        executor, _ = executor_parts(
            h, admission=RetrievalAdmissionService(instance.limiter, namespace=instance.namespace)
        )
        denied = await executor.execute(request(h, "disabled"))
        assert denied.status == "REJECTED"
        key = f"wm:{instance.namespace}:limit:retrieval:org:{h.actor.organization_id}"
        assert await instance.client.get(key) is None
        assert await instance.client.get(f"{key}:actor:{h.actor.membership_id}") is None
    finally:
        await instance.aclose()
