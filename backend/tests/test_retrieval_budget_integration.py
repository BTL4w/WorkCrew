"""Paid attempts reserve once, clamp to parent scope, and survive crash/reclaim."""

import asyncio
from uuid import uuid4

import pytest

from tests.retrieval_fixtures import RetrievalHarness, pytestmark, retrieval_harness

__all__ = ["pytestmark", "retrieval_harness"]


@pytest.mark.parametrize(
    ("kind", "maximum", "bounds"),
    [
        ("SEARCH", 2, (0, 0)),
        ("PAGE_READ", 3, (0, 0)),
        ("TOOL", 8, (0, 0)),
        ("NODE", 12, (0, 0)),
        ("MODEL", 2, (6000, 1600)),
    ],
)
@pytest.mark.asyncio
async def test_hard_caps_deny_next_attempt_before_io(
    retrieval_harness: RetrievalHarness,
    kind: str,
    maximum: int,
    bounds: tuple[int, int],
) -> None:
    from app.modules.retrieval.domain.models import (
        PublicPageRequest,
        PublicSearchRequest,
        RetrievalPolicyError,
    )
    from app.modules.retrieval.domain.public_urls import validate_public_url

    h = retrieval_harness
    service = h.service()
    await service.start_run(h.actor, h.run_id, h.lease())
    request = (
        PublicSearchRequest("public planning")
        if kind == "SEARCH"
        else (
            PublicPageRequest(validate_public_url("https://example.org/guide"))
            if kind == "PAGE_READ"
            else None
        )
    )
    for index in range(maximum):
        await service.reserve_attempt(
            h.actor,
            h.run_id,
            str(index),
            1,
            kind,
            lease=h.lease(),
            request=request,
            token_bounds=bounds,
        )
    with pytest.raises(RetrievalPolicyError, match="BUDGET"):
        await service.reserve_attempt(
            h.actor,
            h.run_id,
            "over",
            1,
            kind,
            lease=h.lease(),
            request=request,
            token_bounds=bounds,
        )


@pytest.mark.parametrize("bounds", [(12001, 1), (1, 1601)])
@pytest.mark.asyncio
async def test_token_reservations_cannot_exceed_manifest(
    retrieval_harness: RetrievalHarness,
    bounds: tuple[int, int],
) -> None:
    from app.modules.retrieval.domain.models import RetrievalPolicyError

    h = retrieval_harness
    service = h.service()
    await service.start_run(h.actor, h.run_id, h.lease())
    with pytest.raises(RetrievalPolicyError, match="BUDGET"):
        await service.reserve_attempt(
            h.actor, h.run_id, "model", 1, "MODEL", lease=h.lease(), token_bounds=bounds
        )


@pytest.mark.asyncio
async def test_lower_parent_remaining_budget_is_authoritative(
    retrieval_harness: RetrievalHarness,
) -> None:
    from app.modules.retrieval.domain.models import RetrievalPolicyError

    h = retrieval_harness
    await h.sql(
        ("UPDATE orchestration_runs SET usage=jsonb_set(usage,'{tool_calls}','15') WHERE id=:id"),
        {"id": h.orchestration_id},
    )
    service = h.service()
    state = await service.start_run(h.actor, h.run_id, h.lease())
    assert state.caps["tool_calls"] == 1
    await service.reserve_attempt(h.actor, h.run_id, "tool-1", 1, "TOOL", lease=h.lease())
    with pytest.raises(RetrievalPolicyError, match="BUDGET"):
        await service.reserve_attempt(h.actor, h.run_id, "tool-2", 1, "TOOL", lease=h.lease())


@pytest.mark.asyncio
async def test_concurrent_same_paid_operation_reserves_once(
    retrieval_harness: RetrievalHarness,
) -> None:
    from app.modules.retrieval.domain.models import PublicSearchRequest, RetrievalPolicyError

    h = retrieval_harness
    service = h.service()
    await service.start_run(h.actor, h.run_id, h.lease())
    outcomes = await asyncio.gather(
        *(
            service.reserve_attempt(
                h.actor,
                h.run_id,
                "same",
                1,
                "SEARCH",
                lease=h.lease(),
                request=PublicSearchRequest("public planning"),
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(item, RetrievalPolicyError) for item in outcomes) == 1
    state = await service.start_run(h.actor, h.run_id, h.lease())
    assert state.reserved["provider_requests"] == 1


@pytest.mark.asyncio
async def test_crash_reclaim_keeps_deadline_counters_and_unknown_charge(
    retrieval_harness: RetrievalHarness,
) -> None:
    from app.modules.retrieval.domain.models import PublicSearchRequest, RetrievalPolicyError

    h = retrieval_harness
    service = h.service()
    before = await service.start_run(h.actor, h.run_id, h.lease())
    receipt = await service.reserve_attempt(
        h.actor,
        h.run_id,
        "paid",
        1,
        "SEARCH",
        lease=h.lease(),
        request=PublicSearchRequest("public planning"),
    )
    await h.sql(
        "UPDATE assistant_jobs SET attempt_count=2,locked_by='new-worker' WHERE id=:id",
        {"id": h.job_id},
    )
    after = await service.start_run(h.actor, h.run_id, h.lease("new-worker", 2))
    assert after.deadline == before.deadline
    assert after.reserved["provider_requests"] == 1
    assert after.fencing_token != receipt.fencing_token
    states = await h.sql(
        "SELECT status FROM retrieval_attempt_reservations WHERE agent_run_id=:id", {"id": h.run_id}
    )
    assert states.scalar_one() == "UNKNOWN"
    with pytest.raises(RetrievalPolicyError, match="UNKNOWN"):
        await service.reserve_attempt(
            h.actor,
            h.run_id,
            "retry",
            1,
            "SEARCH",
            lease=h.lease("new-worker", 2),
            request=PublicSearchRequest("public planning"),
        )
    with pytest.raises(RetrievalPolicyError):
        await service.record_attempt_result(h.actor, receipt, lease=h.lease(), metadata={})


@pytest.mark.asyncio
async def test_expired_run_never_resets_its_wall_budget(
    retrieval_harness: RetrievalHarness,
) -> None:
    from app.modules.retrieval.domain.models import RetrievalPolicyError

    h = retrieval_harness
    service = h.service()
    await service.start_run(h.actor, h.run_id, h.lease())
    # Move the original run start backward before creating a second fresh child budget.
    await h.sql(
        ("UPDATE agent_runs SET started_at=clock_timestamp()-interval '26 seconds' WHERE id=:id"),
        {"id": h.run_id},
    )
    with pytest.raises(RetrievalPolicyError, match="DEADLINE"):
        await service.reserve_attempt(h.actor, h.run_id, "node", 1, "NODE", lease=h.lease())


@pytest.mark.asyncio
async def test_model_cannot_expand_public_query_scope(retrieval_harness: RetrievalHarness) -> None:
    from app.modules.retrieval.domain.models import PublicSearchRequest, RetrievalPolicyError

    h = retrieval_harness
    service = h.service()
    await service.start_run(h.actor, h.run_id, h.lease())
    with pytest.raises(RetrievalPolicyError, match="PROVENANCE"):
        await service.reserve_attempt(
            h.actor,
            h.run_id,
            str(uuid4()),
            1,
            "SEARCH",
            lease=h.lease(),
            request=PublicSearchRequest("private project strategy"),
        )


@pytest.mark.asyncio
async def test_provider_receipt_uses_shared_retrieval_deadline(
    retrieval_harness: RetrievalHarness,
) -> None:
    from datetime import timedelta

    from app.modules.retrieval.domain.models import PublicSearchRequest

    h = retrieval_harness
    service = h.service()
    state = await service.start_run(h.actor, h.run_id, h.lease())
    receipt = await service.reserve_attempt(
        h.actor,
        h.run_id,
        "search",
        1,
        "SEARCH",
        lease=h.lease(),
        request=PublicSearchRequest("public planning"),
    )
    assert receipt.deadline <= state.started_at + timedelta(seconds=8)


@pytest.mark.asyncio
async def test_lower_per_attempt_output_cap_is_not_ignored(
    retrieval_harness: RetrievalHarness,
) -> None:
    from app.modules.retrieval.domain.models import RetrievalPolicyError

    h = retrieval_harness
    await h.sql(
        "UPDATE agent_runs SET typed_input=jsonb_set(typed_input,'{limits}',CAST(:limits "
        "AS jsonb)) WHERE id=:id",
        {"id": h.run_id, "limits": '{"max_output_tokens_per_attempt": 100}'},
    )
    service = h.service()
    await service.start_run(h.actor, h.run_id, h.lease())
    with pytest.raises(RetrievalPolicyError, match="BUDGET"):
        await service.reserve_attempt(
            h.actor, h.run_id, "model", 1, "MODEL", lease=h.lease(), token_bounds=(1000, 101)
        )


@pytest.mark.asyncio
async def test_observed_overrun_is_retained_and_blocks_future_paid_io(
    retrieval_harness: RetrievalHarness,
) -> None:
    from app.modules.retrieval.domain.models import RetrievalPolicyError

    h = retrieval_harness
    service = h.service()
    await service.start_run(h.actor, h.run_id, h.lease())
    receipt = await service.reserve_attempt(
        h.actor, h.run_id, "model", 1, "MODEL", lease=h.lease(), token_bounds=(100, 100)
    )
    with pytest.raises(RetrievalPolicyError, match="USAGE_EXCEEDED"):
        await service.record_attempt_result(
            h.actor, receipt, lease=h.lease(), metadata={"input_tokens": 101, "output_tokens": 10}
        )
    row = (
        await h.sql(
            "SELECT status,result_metadata FROM retrieval_attempt_reservations WHERE id=:id",
            {"id": receipt.id},
        )
    ).one()
    assert row.status == "UNKNOWN" and row.result_metadata["input_tokens"] == 101
    with pytest.raises(RetrievalPolicyError, match="UNKNOWN"):
        await service.reserve_attempt(
            h.actor, h.run_id, "another", 1, "MODEL", lease=h.lease(), token_bounds=(100, 100)
        )


@pytest.mark.asyncio
async def test_ninth_provider_request_is_denied_even_with_retry_room(
    retrieval_harness: RetrievalHarness,
) -> None:
    from app.modules.retrieval.domain.models import (
        PublicPageRequest,
        PublicSearchRequest,
        RetrievalPolicyError,
    )
    from app.modules.retrieval.domain.public_urls import validate_public_url

    h = retrieval_harness
    service = h.service()
    await service.start_run(h.actor, h.run_id, h.lease())
    for index in range(4):
        kind = "SEARCH" if index < 2 else "PAGE_READ"
        request = (
            PublicSearchRequest("public planning")
            if index < 2
            else PublicPageRequest(validate_public_url("https://example.org/guide"))
        )
        first = await service.reserve_attempt(
            h.actor, h.run_id, str(index), 1, kind, lease=h.lease(), request=request
        )
        await service.authorize(h.actor, first, request)
        await service.record_attempt_result(
            h.actor,
            first,
            lease=h.lease(),
            metadata={"retryable": True, "safe_code": "PROVIDER_TIMEOUT"},
        )
        second = await service.reserve_attempt(
            h.actor, h.run_id, str(index), 2, kind, lease=h.lease(), request=request
        )
        await service.authorize(h.actor, second, request)
        await service.record_attempt_result(h.actor, second, lease=h.lease(), metadata={})
    with pytest.raises(RetrievalPolicyError, match="BUDGET"):
        await service.reserve_attempt(
            h.actor,
            h.run_id,
            "ninth",
            1,
            "PAGE_READ",
            lease=h.lease(),
            request=PublicPageRequest(validate_public_url("https://example.org/guide")),
        )


@pytest.mark.asyncio
async def test_receipt_cannot_authorize_changed_request_or_dispatch_twice(
    retrieval_harness: RetrievalHarness,
) -> None:
    from app.modules.retrieval.domain.models import PublicSearchRequest, RetrievalPolicyError

    h = retrieval_harness
    service = h.service()
    await service.start_run(h.actor, h.run_id, h.lease())
    query = PublicSearchRequest("public planning")
    receipt = await service.reserve_attempt(
        h.actor, h.run_id, "paid", 1, "SEARCH", lease=h.lease(), request=query
    )
    with pytest.raises(RetrievalPolicyError, match="RECEIPT"):
        await service.authorize(h.actor, receipt, PublicSearchRequest("public planning", limit=1))
    await service.authorize(h.actor, receipt, query)
    with pytest.raises(RetrievalPolicyError, match="IN_PROGRESS"):
        await service.authorize(h.actor, receipt, query)


@pytest.mark.parametrize("blocker", ["orchestration_runs", "assistant_jobs"])
@pytest.mark.asyncio
async def test_authorization_waits_for_execution_locks_before_budget_lock(
    retrieval_harness: RetrievalHarness,
    blocker: str,
) -> None:
    from sqlalchemy import text

    from app.modules.retrieval.domain.models import PublicSearchRequest

    h = retrieval_harness
    service = h.service()
    await service.start_run(h.actor, h.run_id, h.lease())
    request = PublicSearchRequest("public planning")
    receipt = await service.reserve_attempt(
        h.actor,
        h.run_id,
        "lock-order",
        1,
        "SEARCH",
        lease=h.lease(),
        request=request,
    )
    authorization = None
    try:
        async with h.engine.begin() as connection:
            holder = await connection.scalar(text("SELECT pg_backend_pid()"))
            await connection.execute(
                text(f"SELECT id FROM {blocker} WHERE id=:id FOR UPDATE"),
                {"id": h.orchestration_id if blocker == "orchestration_runs" else h.job_id},
            )
            authorization = asyncio.create_task(service.authorize(h.actor, receipt, request))
            waiting = 0
            for _ in range(100):
                waiting = (
                    await h.sql(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE :holder=ANY(pg_blocking_pids(pid))",
                        {"holder": holder},
                    )
                ).scalar_one()
                if waiting:
                    break
                await asyncio.sleep(0.01)
            assert waiting, "authorization should be waiting for the held execution row"
            # Execution/finalization already owns that row. It must still be able to
            # lock the budget; otherwise authorization introduces an inverted order.
            await connection.execute(
                text(
                    "SELECT * FROM retrieval_run_budgets WHERE agent_run_id=:run FOR UPDATE NOWAIT"
                ),
                {"run": h.run_id},
            )
        await authorization
    finally:
        if authorization is not None and not authorization.done():
            authorization.cancel()
            await asyncio.gather(authorization, return_exceptions=True)


@pytest.mark.asyncio
async def test_operational_accounting_keeps_hashes_without_public_query_copies(
    retrieval_harness: RetrievalHarness,
) -> None:
    from app.modules.retrieval.domain.models import PublicSearchRequest, RetrievalPolicyError

    h = retrieval_harness
    service = h.service()
    await service.start_run(h.actor, h.run_id, h.lease())
    receipt = await service.reserve_attempt(
        h.actor,
        h.run_id,
        "safe-query",
        1,
        "SEARCH",
        lease=h.lease(),
        request=PublicSearchRequest("public planning"),
    )
    with pytest.raises(RetrievalPolicyError):
        await service.record_attempt_result(
            h.actor, receipt, lease=h.lease(), metadata={"raw_error": "secret-provider-error"}
        )
    for table in ("retrieval_run_budgets", "retrieval_attempt_reservations"):
        rows = await h.sql(
            f"SELECT to_jsonb(row)::text FROM {table} row WHERE agent_run_id=:id", {"id": h.run_id}
        )
        serialized = " ".join(row[0] for row in rows)
        assert "public planning" not in serialized
        assert "https://example.org/guide" not in serialized
        assert "secret-provider-error" not in serialized
    audit = await h.sql(
        "SELECT reason_data::text FROM audit_events WHERE organization_id=:org",
        {"org": h.actor.organization_id},
    )
    assert "secret-provider-error" not in " ".join(row[0] for row in audit)


@pytest.mark.parametrize(
    ("limits", "first", "second"),
    [
        ({}, (8000, 100), (5000, 100)),
        ({"max_output_tokens": 2500}, (100, 1600), (100, 1000)),
    ],
)
@pytest.mark.asyncio
async def test_cumulative_tokens_are_enforced_before_second_model_attempt(
    retrieval_harness: RetrievalHarness,
    limits: dict[str, int],
    first: tuple[int, int],
    second: tuple[int, int],
) -> None:
    import json

    from app.modules.retrieval.domain.models import RetrievalPolicyError

    h = retrieval_harness
    await h.sql(
        "UPDATE agent_runs SET typed_input=typed_input||CAST(:input AS jsonb) WHERE id=:id",
        {"id": h.run_id, "input": json.dumps({"limits": limits})},
    )
    service = h.service()
    await service.start_run(h.actor, h.run_id, h.lease())
    await service.reserve_attempt(
        h.actor, h.run_id, "first-model", 1, "MODEL", lease=h.lease(), token_bounds=first
    )
    with pytest.raises(RetrievalPolicyError, match="BUDGET"):
        await service.reserve_attempt(
            h.actor, h.run_id, "second-model", 1, "MODEL", lease=h.lease(), token_bounds=second
        )


@pytest.mark.asyncio
async def test_unknown_parent_usage_is_not_assumed_zero(
    retrieval_harness: RetrievalHarness,
) -> None:
    from app.modules.retrieval.domain.models import RetrievalPolicyError

    h = retrieval_harness
    await h.sql("UPDATE orchestration_runs SET usage='{}' WHERE id=:id", {"id": h.orchestration_id})
    with pytest.raises(RetrievalPolicyError, match="PARENT_BUDGET_UNAVAILABLE"):
        await h.service().start_run(h.actor, h.run_id, h.lease())
    assert (
        await h.sql(
            "SELECT count(*) FROM retrieval_run_budgets WHERE agent_run_id=:id", {"id": h.run_id}
        )
    ).scalar_one() == 0


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.asyncio
async def test_vietnamese_public_scope_and_private_query_refusal(
    retrieval_harness: RetrievalHarness,
    private: bool,
) -> None:
    import json

    from app.modules.retrieval.domain.models import PublicSearchRequest, RetrievalPolicyError

    h = retrieval_harness
    query = "lập kế hoạch nội bộ" if private else "lập kế hoạch"
    message = f"Tra cứu web công khai về {query}"
    await h.sql(
        "UPDATE assistant_messages SET content_blocks=CAST(:body AS jsonb) WHERE id=:id",
        {"id": h.message_id, "body": json.dumps([{"kind": "text", "text": message}])},
    )
    await h.sql(
        "UPDATE agent_runs SET typed_input=jsonb_set(typed_input,'{query}',"
        "CAST(:query AS jsonb)) WHERE id=:id",
        {"id": h.run_id, "query": json.dumps(query)},
    )
    service = h.service()
    if private:
        with pytest.raises(RetrievalPolicyError, match="PROVENANCE"):
            await service.start_run(h.actor, h.run_id, h.lease())
    else:
        await service.start_run(h.actor, h.run_id, h.lease())
        receipt = await service.reserve_attempt(
            h.actor,
            h.run_id,
            "vi-query",
            1,
            "SEARCH",
            lease=h.lease(),
            request=PublicSearchRequest(query),
        )
        await service.authorize(h.actor, receipt, PublicSearchRequest(query))


@pytest.mark.asyncio
async def test_claim_expiring_while_waiting_for_parent_cannot_dispatch(
    retrieval_harness: RetrievalHarness,
) -> None:
    from sqlalchemy import text

    from app.modules.retrieval.domain.models import PublicSearchRequest, RetrievalPolicyError

    h = retrieval_harness
    service = h.service()
    await service.start_run(h.actor, h.run_id, h.lease())
    request = PublicSearchRequest("public planning")
    receipt = await service.reserve_attempt(
        h.actor,
        h.run_id,
        "expiring-claim",
        1,
        "SEARCH",
        lease=h.lease(),
        request=request,
    )
    await h.sql(
        "UPDATE assistant_jobs SET lease_until=clock_timestamp()+interval '2 seconds' WHERE id=:id",
        {"id": h.job_id},
    )
    authorization = None
    try:
        async with h.engine.begin() as connection:
            holder = await connection.scalar(text("SELECT pg_backend_pid()"))
            await connection.execute(
                text("SELECT id FROM orchestration_runs WHERE id=:id FOR UPDATE"),
                {"id": h.orchestration_id},
            )
            authorization = asyncio.create_task(service.authorize(h.actor, receipt, request))
            waiting = 0
            for _ in range(100):
                waiting = (
                    await h.sql(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE :holder=ANY(pg_blocking_pids(pid))",
                        {"holder": holder},
                    )
                ).scalar_one()
                if waiting:
                    break
                await asyncio.sleep(0.01)
            assert waiting
            await asyncio.sleep(2.1)
        with pytest.raises(RetrievalPolicyError, match="JOB_CLAIM_LOST"):
            await authorization
        assert (
            await h.sql(
                "SELECT dispatched FROM retrieval_attempt_reservations WHERE id=:id",
                {"id": receipt.id},
            )
        ).scalar_one() is False
    finally:
        if authorization is not None and not authorization.done():
            authorization.cancel()
            await asyncio.gather(authorization, return_exceptions=True)
