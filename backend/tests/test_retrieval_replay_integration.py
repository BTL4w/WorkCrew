"""Real durable tool results are permission/expiry checked before terminal replay."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.retrieval_fixtures import RetrievalHarness, pytestmark, retrieval_harness

if TYPE_CHECKING:
    from app.modules.assistant.adapters.retrieval_tools import RetrievalToolExecutor
    from app.modules.assistant.adapters.work_tools import RecordingToolExecutor
    from app.modules.retrieval.adapters.mock import MockRetrievalAdapter
    from work_management_ai.runtime.contracts import ToolExecutionRequest

__all__ = ["pytestmark", "retrieval_harness"]


def _request(h: RetrievalHarness, call: str = "search-1") -> ToolExecutionRequest:
    from work_management_ai.runtime.contracts import ActorReference, ToolExecutionRequest

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


def _executor_parts(
    h: RetrievalHarness,
    provider: MockRetrievalAdapter | None = None,
    *,
    revalidate: bool = True,
) -> tuple[RecordingToolExecutor, RetrievalToolExecutor]:
    from app.core.database import create_session_factory
    from app.modules.assistant.adapters.retrieval_tools import RetrievalToolExecutor
    from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory
    from app.modules.assistant.adapters.work_tools import RecordingToolExecutor
    from app.modules.identity.adapters.auth_repository import SqlAlchemyAuthTransactionFactory
    from app.modules.identity.adapters.current_actor import CurrentActorResolver
    from app.modules.identity.application.current_actor_service import CurrentActorService
    from app.modules.retrieval.adapters.mock import MockRetrievalAdapter
    from app.modules.retrieval.application.service import RetrievalService
    from work_management_ai.runtime.manifests import ToolManifest, load_yaml_resource
    from work_management_ai.runtime.tool_registry import ToolRegistry

    class DNS:
        async def resolve(self, hostname: str) -> tuple[str, ...]:
            return ("93.184.216.34",)

    sessions = create_session_factory(h.engine)
    registry = ToolRegistry(
        load_yaml_resource("work_management_ai.tools.web", name, ToolManifest)
        for name in ("search.yaml", "read_pages.yaml", "revalidate.yaml")
    )
    budget = h.service()
    adapter = provider or MockRetrievalAdapter()
    backend = RetrievalToolExecutor(
        budgets=budget,
        service=RetrievalService(search=adapter, pages=adapter, access=budget, dns=DNS()),
        actor_resolver=CurrentActorResolver(
            CurrentActorService(SqlAlchemyAuthTransactionFactory(sessions))
        ),
        tool_registry=registry,
        lease=h.lease(),
    )
    recording = RecordingToolExecutor(
        transaction_factory=PostgreSQLAssistantTransactionFactory(sessions),
        tool_registry=registry,
        backend=backend,
        replay_validator=backend if revalidate else None,
        job_claim=h.job,
    )
    return recording, backend


def _executor(h: RetrievalHarness) -> RecordingToolExecutor:
    return _executor_parts(h)[0]


@pytest.mark.asyncio
async def test_search_persists_app_owned_snapshot_and_replays_without_new_charge(
    retrieval_harness: RetrievalHarness,
) -> None:
    h = retrieval_harness
    executor = _executor(h)
    request = _request(h)
    first = await executor.execute(request)
    assert first.status == "SUCCEEDED", first.safe_error_code
    assert len(first.evidence) == 1 and first.evidence[0].resource_type == "WEB_SOURCE_SNAPSHOT"
    second = await executor.execute(request)
    assert second == first
    rows = await h.sql(
        "SELECT reserved FROM retrieval_run_budgets WHERE agent_run_id=:id", {"id": h.run_id}
    )
    assert rows.scalar_one()["provider_requests"] == 1
    rows = await h.sql(
        "SELECT count(*) FROM retrieval_source_snapshots WHERE agent_run_id=:id", {"id": h.run_id}
    )
    assert rows.scalar_one() == 1


@pytest.mark.parametrize(
    "reason", ["disabled", "revoked", "expired", "wrong-hash", "peer", "lease"]
)
@pytest.mark.asyncio
async def test_terminal_result_is_not_cached_authority(
    retrieval_harness: RetrievalHarness, reason: str
) -> None:
    from app.modules.retrieval.domain.models import RetrievalPolicyError

    h = retrieval_harness
    executor = _executor(h)
    request = _request(h)
    result = await executor.execute(request)
    assert result.status == "SUCCEEDED", result.safe_error_code
    before = (
        await h.sql(
            "SELECT typed_output FROM tool_invocations WHERE agent_run_id=:id", {"id": h.run_id}
        )
    ).scalar_one()
    if reason == "disabled":
        h.enabled = False
    elif reason == "revoked":
        await h.sql(
            "UPDATE memberships SET is_active=false WHERE id=:id", {"id": h.actor.membership_id}
        )
    elif reason == "lease":
        await h.sql(
            "UPDATE assistant_jobs SET locked_by='new-worker',attempt_count=2 WHERE id=:id",
            {"id": h.job_id},
        )
    elif reason == "expired":
        # Expire via the configured retention boundary, never mutate an immutable snapshot.
        from datetime import UTC, datetime, timedelta

        from app.core.database import create_session_factory
        from app.modules.feedback.adapters.payload_repository import RetentionTransactions
        from app.modules.feedback.application.retention_service import RetentionService

        await RetentionService(RetentionTransactions(create_session_factory(h.engine))).purge_once(
            organization_id=h.actor.organization_id, now=datetime.now(UTC) + timedelta(days=31)
        )
    elif reason in {"wrong-hash", "peer"}:
        with pytest.raises(RetrievalPolicyError):
            await h.service().load_evidence(
                h.peer if reason == "peer" else h.actor,
                tuple(item.resource_id for item in result.evidence),
                expected_hashes=(result.evidence[0].fingerprint or "",)
                if reason == "peer"
                else ("b" * 64,),
            )
        return
    replay = await executor.execute(request)
    assert replay.status == "REJECTED" and replay.evidence == () and replay.typed_output == {}
    after = (
        await h.sql(
            "SELECT typed_output FROM tool_invocations WHERE agent_run_id=:id", {"id": h.run_id}
        )
    ).scalar_one()
    if reason == "expired":
        assert after is None
    else:
        assert after == before


@pytest.mark.asyncio
async def test_snapshot_and_terminal_attempt_are_immutable(
    retrieval_harness: RetrievalHarness,
) -> None:
    from sqlalchemy.exc import DBAPIError

    h = retrieval_harness
    result = await _executor(h).execute(_request(h))
    assert result.status == "SUCCEEDED", result.safe_error_code
    for sql in (
        "UPDATE retrieval_source_snapshots SET payload='{}' WHERE agent_run_id=:id",
        "UPDATE retrieval_attempt_reservations SET status='RESERVED' WHERE agent_run_id=:id",
    ):
        with pytest.raises(DBAPIError):
            await h.sql(sql, {"id": h.run_id})


@pytest.mark.asyncio
async def test_revalidate_tool_keeps_original_source_producer_binding(
    retrieval_harness: RetrievalHarness,
) -> None:
    h = retrieval_harness
    executor = _executor(h)
    found = await executor.execute(_request(h))
    assert found.status == "SUCCEEDED", found.safe_error_code
    source = found.evidence[0]
    request = _request(h, "verify").model_copy(
        update={
            "tool_id": "web.revalidate",
            "typed_input": {
                "sources": [
                    {"evidence_id": str(source.resource_id), "fingerprint": source.fingerprint}
                ]
            },
        }
    )
    result = await executor.execute(request)
    assert result.status == "SUCCEEDED", result.safe_error_code
    assert result.typed_output["valid_evidence_ids"] == [str(source.resource_id)]
    assert (await executor.execute(request)) == result


@pytest.mark.asyncio
async def test_lease_lost_during_io_cannot_publish_a_snapshot(
    retrieval_harness: RetrievalHarness,
) -> None:
    from app.modules.retrieval.adapters.mock import MockRetrievalAdapter
    from app.modules.retrieval.domain.models import PublicSearchRequest, SearchBatch

    h = retrieval_harness

    class Reclaimed(MockRetrievalAdapter):
        async def search(
            self, request: PublicSearchRequest, *, timeout_seconds: float
        ) -> SearchBatch:
            await h.sql(
                "UPDATE assistant_jobs SET locked_by='replacement',attempt_count=2 WHERE id=:id",
                {"id": h.job_id},
            )
            return await super().search(request, timeout_seconds=timeout_seconds)

    executor, _ = _executor_parts(h, Reclaimed())
    result = await executor.execute(_request(h))
    assert result.status == "REJECTED" and result.evidence == ()
    assert (
        await h.sql(
            "SELECT count(*) FROM retrieval_source_snapshots WHERE agent_run_id=:id",
            {"id": h.run_id},
        )
    ).scalar_one() == 0


@pytest.mark.asyncio
async def test_concurrent_recorded_call_does_not_finalize_the_active_owner(
    retrieval_harness: RetrievalHarness,
) -> None:
    import asyncio

    from app.modules.retrieval.adapters.mock import MockRetrievalAdapter
    from app.modules.retrieval.domain.models import PublicSearchRequest, SearchBatch

    h = retrieval_harness
    started, release = asyncio.Event(), asyncio.Event()

    class Waiting(MockRetrievalAdapter):
        async def search(
            self, request: PublicSearchRequest, *, timeout_seconds: float
        ) -> SearchBatch:
            started.set()
            await release.wait()
            return await super().search(request, timeout_seconds=timeout_seconds)

    executor, _ = _executor_parts(h, Waiting())
    request = _request(h)
    first = asyncio.create_task(executor.execute(request))
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        second = await executor.execute(request)
        assert second.safe_error_code == "RETRIEVAL_ATTEMPT_IN_PROGRESS"
    finally:
        release.set()
    assert (await first).status == "SUCCEEDED"
    counts = (
        await h.sql(
            "SELECT reserved FROM retrieval_run_budgets WHERE agent_run_id=:id", {"id": h.run_id}
        )
    ).scalar_one()
    assert counts["tool_calls"] == 1 and counts["provider_requests"] == 1


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.asyncio
async def test_completed_specialist_result_cannot_bypass_source_revalidation(
    retrieval_harness: RetrievalHarness, configured: bool
) -> None:
    from datetime import UTC, datetime
    from typing import cast
    from uuid import NAMESPACE_URL, uuid5

    from app.core.database import create_session_factory
    from app.modules.assistant.adapters.agent_runtime import build_agent_registry
    from app.modules.assistant.adapters.execution_recorder import PostgreSQLExecutionRecorder
    from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory
    from work_management_ai.agents.retrieve.contracts import RetrieveOutput
    from work_management_ai.runtime.contracts import (
        AgentBudget,
        AgentHandoff,
        AgentId,
        AgentResult,
        AgentRunStatus,
        JsonValue,
    )

    h = retrieval_harness
    executor, backend = _executor_parts(h)
    found = await executor.execute(_request(h))
    assert found.status == "SUCCEEDED", found.safe_error_code
    output = RetrieveOutput.model_validate(
        {
            "outcome": "EVIDENCE_READY",
            "strategy_used": "WEB",
            "evidence": found.typed_output["evidence"],
            "observed_at": datetime.now(UTC),
        }
    )
    result = AgentResult(
        agent_id=AgentId.RETRIEVE,
        agent_version="1.0.0",
        status=AgentRunStatus.COMPLETED,
        typed_output=cast(dict[str, JsonValue], output.model_dump(mode="json")),
        evidence=found.evidence,
        stop_reason="EVIDENCE_READY",
    )
    await h.sql(
        (
            "UPDATE agent_runs SET status='COMPLETED',typed_output=CAST(:result AS "
            "jsonb),completed_at=clock_timestamp() WHERE id=:id"
        ),
        {"id": h.run_id, "result": result.model_dump_json()},
    )
    registry, _ = build_agent_registry()
    recorder = PostgreSQLExecutionRecorder(
        transaction_factory=PostgreSQLAssistantTransactionFactory(create_session_factory(h.engine)),
        registry=registry,
        job=h.job,
        retrieval_replay_validator=backend if configured else None,
    )
    handoff = AgentHandoff(
        orchestration_run_id=h.orchestration_id,
        parent_agent_run_id=uuid5(NAMESPACE_URL, f"orchestrator:{h.job.turn_id}"),
        target_agent_id=AgentId.RETRIEVE,
        target_agent_version="1.0.0",
        capability="retrieval.search_web",
        objective="Public planning",
        actor=_request(h).actor,
        typed_input={"query": "public planning"},
        context_references=(),
        budget=AgentBudget(max_iterations=12, max_tool_calls=8, timeout_seconds=25),
        step_id="retrieve-1",
        idempotency_key=f"{h.job.turn_id}:retrieve-1",
    )
    replay = await recorder.start_agent_run(handoff)
    if configured:
        assert replay.replayed_result == result
        h.enabled = False
        replay = await recorder.start_agent_run(handoff)
    assert (
        replay.replayed_result is not None
        and replay.replayed_result.status == AgentRunStatus.FAILED
    )
    assert replay.replayed_result.evidence == () and replay.replayed_result.typed_output == {}
    assert (
        await h.sql("SELECT typed_output FROM agent_runs WHERE id=:id", {"id": h.run_id})
    ).scalar_one() == result.model_dump(mode="json")


@pytest.mark.asyncio
async def test_snapshot_ttl_and_all_owner_rls_boundaries(
    retrieval_harness: RetrievalHarness,
) -> None:
    import hashlib
    import json
    from uuid import uuid4

    from sqlalchemy import text

    from app.modules.retrieval.domain.models import RetrievalPolicyError

    h = retrieval_harness
    found = await _executor(h).execute(_request(h))
    assert found.status == "SUCCEEDED", found.safe_error_code
    for actor in (h.peer, h.foreign):
        async with h.engine.connect() as connection, connection.begin():
            await connection.execute(text("SET LOCAL ROLE app_runtime"))
            await connection.execute(
                text(
                    "SELECT "
                    "set_config('app.organization_id',:org,true),set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            for table in (
                "retrieval_run_budgets",
                "retrieval_attempt_reservations",
                "retrieval_source_snapshots",
            ):
                assert list(await connection.execute(text(f"SELECT * FROM {table}"))) == []
        with pytest.raises(RetrievalPolicyError):
            await h.service().load_evidence(
                actor,
                (found.evidence[0].resource_id,),
                expected_hashes=(found.evidence[0].fingerprint or "",),
            )
    expired = uuid4()
    fingerprint = hashlib.sha256(b"expired evidence").hexdigest()
    await h.sql(
        (
            "INSERT INTO "
            "retrieval_source_snapshots(organization_id,id,owner_membership_id,agent_run_id,tool_invocation_id,canonical_url,fingerprint,payload,observed_at,expires_at)"
            " SELECT "
            "organization_id,:id,owner_membership_id,agent_run_id,tool_invocation_id,'https://example.org/expired',:hash,payload||CAST(:text"
            " AS jsonb),clock_timestamp()-interval '1 "
            "day',clock_timestamp()-interval '1 second' FROM "
            "retrieval_source_snapshots WHERE id=:source"
        ),
        {
            "id": expired,
            "hash": fingerprint,
            "text": json.dumps({"text": "expired evidence"}),
            "source": found.evidence[0].resource_id,
        },
    )
    with pytest.raises(RetrievalPolicyError, match="SOURCE_UNAVAILABLE"):
        await h.service().load_evidence(h.actor, (expired,), expected_hashes=(fingerprint,))


@pytest.mark.parametrize("resource", ["budget", "attempt", "snapshot"])
@pytest.mark.asyncio
async def test_same_organization_foreign_keys_reject_forged_references(
    retrieval_harness: RetrievalHarness, resource: str
) -> None:
    from uuid import uuid4

    from sqlalchemy.exc import DBAPIError

    h = retrieval_harness
    assert (await _executor(h).execute(_request(h))).status == "SUCCEEDED"
    if resource == "budget":
        sql = (
            "INSERT INTO retrieval_run_budgets SELECT "
            "organization_id,:new_run,owner_membership_id,orchestration_run_id,job_id,worker_id,job_attempt,fencing_token,scope,caps,reserved,started_at,deadline,expires_at,version"
            " FROM retrieval_run_budgets WHERE agent_run_id=:run"
        )
    elif resource == "attempt":
        sql = (
            "INSERT INTO retrieval_attempt_reservations SELECT "
            "organization_id,:id,agent_run_id,:foreign_owner,operation_key||'-forged',attempt_no,kind,fencing_token,request_hash,charged,status,dispatched,result_metadata,cost_usd,deadline,created_at,completed_at"
            " FROM retrieval_attempt_reservations WHERE agent_run_id=:run LIMIT 1"
        )
    else:
        sql = (
            "INSERT INTO retrieval_source_snapshots SELECT "
            "organization_id,:id,owner_membership_id,agent_run_id,:foreign_tool,canonical_url,fingerprint,payload,observed_at,expires_at"
            " FROM retrieval_source_snapshots WHERE agent_run_id=:run LIMIT 1"
        )
    with pytest.raises(DBAPIError) as error:
        await h.sql(
            sql,
            {
                "run": h.run_id,
                "new_run": uuid4(),
                "id": uuid4(),
                "foreign_owner": h.foreign.membership_id,
                "foreign_tool": uuid4(),
            },
        )
    assert getattr(error.value.orig, "sqlstate", None) == "23503"


@pytest.mark.asyncio
async def test_replay_rejects_a_source_copy_with_changed_text(
    retrieval_harness: RetrievalHarness,
) -> None:
    from datetime import UTC, datetime
    from typing import cast

    from work_management_ai.agents.retrieve.contracts import RetrieveOutput
    from work_management_ai.runtime.contracts import AgentId, AgentResult, AgentRunStatus, JsonValue

    h = retrieval_harness
    executor, backend = _executor_parts(h)
    found = await executor.execute(_request(h))
    assert found.status == "SUCCEEDED", found.safe_error_code
    output = RetrieveOutput.model_validate(
        {
            "outcome": "EVIDENCE_READY",
            "strategy_used": "WEB",
            "evidence": found.typed_output["evidence"],
            "observed_at": datetime.now(UTC),
        }
    )
    body = output.model_dump(mode="json")
    body["evidence"][0]["excerpt"] = "A replaced source body must not inherit the old binding"
    result = AgentResult(
        agent_id=AgentId.RETRIEVE,
        agent_version="1.0.0",
        status=AgentRunStatus.COMPLETED,
        typed_output=cast(dict[str, JsonValue], body),
        evidence=found.evidence,
        stop_reason="EVIDENCE_READY",
    )
    checked = await backend.revalidate_agent_result(h.run_id, _request(h).actor, result)
    assert (
        checked.status == AgentRunStatus.FAILED
        and checked.evidence == ()
        and checked.typed_output == {}
    )


@pytest.mark.asyncio
async def test_observation_clock_skew_keeps_snapshot_within_retention(
    retrieval_harness: RetrievalHarness,
) -> None:
    from dataclasses import replace
    from datetime import timedelta

    from app.modules.retrieval.adapters.mock import MockRetrievalAdapter
    from app.modules.retrieval.domain.models import PublicSearchRequest, SearchBatch

    class EarlierClock(MockRetrievalAdapter):
        async def search(
            self, request: PublicSearchRequest, *, timeout_seconds: float
        ) -> SearchBatch:
            batch = await super().search(request, timeout_seconds=timeout_seconds)
            return replace(
                batch,
                sources=tuple(
                    replace(source, observed_at=source.observed_at - timedelta(seconds=30))
                    for source in batch.sources
                ),
            )

    h = retrieval_harness
    executor, _ = _executor_parts(h, EarlierClock())
    result = await executor.execute(_request(h))
    assert result.status == "SUCCEEDED", result.safe_error_code
    row = (
        await h.sql(
            (
                "SELECT observed_at,expires_at FROM retrieval_source_snapshots "
                "WHERE agent_run_id=:id"
            ),
            {"id": h.run_id},
        )
    ).one()
    assert row.expires_at <= row.observed_at + timedelta(days=30)


@pytest.mark.asyncio
async def test_cached_tool_rejects_changed_source_copy(retrieval_harness: RetrievalHarness) -> None:
    from work_management_ai.tools.web.contracts import WebSearchOutput

    h = retrieval_harness
    executor, backend = _executor_parts(h)
    found = await executor.execute(_request(h))
    assert found.status == "SUCCEEDED", found.safe_error_code
    body = WebSearchOutput.model_validate(found.typed_output).model_dump(mode="json")
    body["evidence"][0]["title"] = "Injected title with unchanged source fingerprint"
    changed = found.model_copy(update={"typed_output": body})
    checked = await backend.revalidate_cached(_request(h), changed)
    assert checked.status == "REJECTED" and checked.typed_output == {} and checked.evidence == ()


@pytest.mark.parametrize("identity", ["peer", "foreign"])
@pytest.mark.parametrize(
    "table",
    [
        "retrieval_run_budgets",
        "retrieval_attempt_reservations",
        "retrieval_source_snapshots",
    ],
)
@pytest.mark.asyncio
async def test_rls_rejects_copying_other_owner_rows(
    retrieval_harness: RetrievalHarness,
    identity: str,
    table: str,
) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    h = retrieval_harness
    assert (await _executor(h).execute(_request(h))).status == "SUCCEEDED"
    row = (
        await h.sql(
            f"SELECT to_jsonb(row)::text FROM {table} row WHERE agent_run_id=:run LIMIT 1",
            {"run": h.run_id},
        )
    ).scalar_one()
    actor = h.peer if identity == "peer" else h.foreign
    async with h.engine.connect() as connection, connection.begin():
        await connection.execute(text("SET LOCAL ROLE app_runtime"))
        await connection.execute(
            text(
                "SELECT set_config('app.organization_id',:org,true),"
                "set_config('app.membership_id',:member,true)"
            ),
            {"org": str(actor.organization_id), "member": str(actor.membership_id)},
        )
        with pytest.raises(DBAPIError) as error:
            async with connection.begin_nested():
                await connection.execute(
                    text(
                        f"INSERT INTO {table} SELECT * FROM "
                        f"jsonb_populate_record(NULL::{table}, CAST(:row AS jsonb))"
                    ),
                    {"row": row},
                )
        assert getattr(error.value.orig, "sqlstate", None) == "42501"


@pytest.mark.asyncio
async def test_lower_source_cap_applies_to_persisted_evidence(
    retrieval_harness: RetrievalHarness,
) -> None:
    import json
    from dataclasses import replace

    from app.modules.retrieval.adapters.mock import MockRetrievalAdapter
    from app.modules.retrieval.domain.models import PublicSearchRequest, SearchBatch
    from app.modules.retrieval.domain.public_urls import validate_public_url

    class TwoSources(MockRetrievalAdapter):
        async def search(
            self, request: PublicSearchRequest, *, timeout_seconds: float
        ) -> SearchBatch:
            batch = await super().search(request, timeout_seconds=timeout_seconds)
            return replace(
                batch,
                sources=(
                    *batch.sources,
                    replace(
                        batch.sources[0], url=validate_public_url("https://example.org/second")
                    ),
                ),
            )

    h = retrieval_harness
    await h.sql(
        "UPDATE agent_runs SET typed_input=typed_input||CAST(:limits AS jsonb) WHERE id=:id",
        {"id": h.run_id, "limits": json.dumps({"limits": {"max_sources": 1}})},
    )
    executor, _ = _executor_parts(h, TwoSources())
    result = await executor.execute(_request(h))
    assert result.status == "SUCCEEDED", result.safe_error_code
    assert len(result.evidence) == 1
    from work_management_ai.tools.web.contracts import WebSearchOutput

    assert "SOURCE_LIMIT_REACHED" in WebSearchOutput.model_validate(result.typed_output).limitations


@pytest.mark.asyncio
async def test_web_executor_without_revalidator_fails_before_io(
    retrieval_harness: RetrievalHarness,
) -> None:
    h = retrieval_harness
    executor, _ = _executor_parts(h, revalidate=False)
    result = await executor.execute(_request(h))
    assert result.status == "REJECTED"
    assert result.safe_error_code == "RETRIEVAL_REVALIDATION_REQUIRED"
    assert result.typed_output == {} and result.evidence == ()
    assert (
        await h.sql(
            "SELECT count(*) FROM retrieval_attempt_reservations WHERE agent_run_id=:id",
            {"id": h.run_id},
        )
    ).scalar_one() == 0
