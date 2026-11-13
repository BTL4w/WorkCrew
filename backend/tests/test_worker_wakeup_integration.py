"""Real commit hints, SQL claims, due times and fencing remain independent."""

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.database import create_session_factory
from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory
from app.modules.assistant.application.job_service import AssistantJobService
from app.modules.assistant.application.service import AssistantService
from app.modules.assistant.domain.models import AssistantJob
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.optimization.adapters.redis_runtime import create_redis_runtime
from app.modules.optimization.application.wakeup import WakeupPort
from app.modules.planning_runs.adapters.transaction import PostgreSQLPlanningRunTransactionFactory
from app.modules.planning_runs.domain.models import WorkflowJob, WorkflowJobStatus
from app.worker import worker_wakeups
from tests.retrieval_fixtures import RetrievalHarness, pytestmark, retrieval_harness
from tests.test_planning_run_api_integration import run_for
from tests.test_sse_wakeup_integration import Hints

__all__ = ["pytestmark", "retrieval_harness"]


class PlanningSnapshot:
    async def get_proposal_version(
        self, *, actor: AuthenticatedActor, proposal_id: UUID
    ) -> int | None:
        return 1


def service(h: RetrievalHarness, wake: WakeupPort | None = None):
    return AssistantService(
        transaction_factory=PostgreSQLAssistantTransactionFactory(
            create_session_factory(h.engine), wakeup=wake
        ),
        planning_snapshot=PlanningSnapshot(),
        orchestrator_version="1.0.0",
        orchestrator_fingerprint="worker-wakeup-test",
    )


async def enqueue(h: RetrievalHarness, wake: WakeupPort | None = None, key: str | None = None):
    return await service(h, wake).post_message(
        actor=h.actor,
        conversation_id=h.job.conversation_id,
        message="Summarize my tasks",
        card_action=None,
        if_match_version=None,
        locale="en",
        request_id=uuid4().hex,
        idempotency_key=key or uuid4().hex,
    )


@pytest.mark.asyncio
async def test_after_commit_job_hint_and_rollback_are_bound_to_sql_transaction(
    retrieval_harness: RetrievalHarness,
):
    h = retrieval_harness
    hints = Hints()
    factory = PostgreSQLPlanningRunTransactionFactory(
        create_session_factory(h.engine), wakeup=hints
    )
    now = datetime.now(UTC)
    run = replace(run_for(h.actor), created_at=now, updated_at=now)
    job = WorkflowJob(
        id=uuid4(),
        organization_id=h.actor.organization_id,
        workflow_run_id=run.id,
        job_type="planning.start",
        status=WorkflowJobStatus.QUEUED,
        payload={},
        available_at=now,
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(ValueError, match="rollback"):
        async with factory(h.actor) as tx:
            await tx.repository.create_workflow_run(run=run, job=job)
            assert hints.hints == []
            raise ValueError("rollback")
    assert hints.hints == []
    async with factory(h.actor) as tx:
        await tx.repository.create_workflow_run(run=run, job=job)
        assert hints.hints == []
    assert len(hints.hints) == 1 and hints.hints[0].kind == "job.ready"
    assert hints.hints[0].organization_id == h.actor.organization_id


@pytest.mark.asyncio
async def test_missed_hint_still_claims_exact_job_after_poll_and_future_job_waits(
    retrieval_harness: RetrievalHarness,
):
    h = retrieval_harness
    result = await enqueue(h)
    await h.sql(
        "UPDATE assistant_jobs SET available_at=now()+interval '1 day' WHERE id=:id",
        {"id": result.job.id},
    )
    handled: list[AssistantJob] = []

    async def handle(*, job: AssistantJob, worker_id: str):
        handled.append(job)

    jobs = AssistantJobService(
        transaction_factory=PostgreSQLAssistantTransactionFactory(create_session_factory(h.engine)),
        handler=handle,
        organization_scopes={h.actor.organization_id},
    )
    async with worker_wakeups(None, [h.actor.organization_id]) as wake:
        assert not await jobs.run_once(worker_id="poll", organization_id=h.actor.organization_id)
        await h.sql(
            "UPDATE assistant_jobs SET available_at=now()-interval '1 second' WHERE id=:id",
            {"id": result.job.id},
        )
        await wake.wait(timeout_seconds=0.01)
        assert await jobs.run_once(worker_id="poll", organization_id=h.actor.organization_id)
    assert [job.id for job in handled] == [result.job.id]
    row = await h.sql(
        "SELECT status,locked_by FROM assistant_jobs WHERE id=:id", {"id": result.job.id}
    )
    assert row.one() == ("COMPLETED", None)


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("RUN_REDIS_INTEGRATION") != "1", reason="requires Redis")
async def test_committed_hint_wakes_two_workers_with_one_sql_effect(
    retrieval_harness: RetrievalHarness,
):
    h = retrieval_harness
    runtime = create_redis_runtime(
        Settings(
            environment="test",
            redis_wakeup_enabled=True,
            redis_url=SecretStr(os.environ["REDIS_TEST_URL"]),
            redis_namespace="worker-" + uuid4().hex[:20],
        )
    )
    effects: list[AssistantJob] = []

    async def handle(*, job: AssistantJob, worker_id: str):
        effects.append(job)
        async with PostgreSQLAssistantTransactionFactory(create_session_factory(h.engine))(
            h.actor
        ) as tx:
            await tx.repository.append_assistant_blocks(
                job=job,
                blocks=({"kind": "text", "text": "One worker response"},),
                dedupe_key="worker-wake-once",
            )

    jobs = AssistantJobService(
        transaction_factory=PostgreSQLAssistantTransactionFactory(create_session_factory(h.engine)),
        handler=handle,
        organization_scopes={h.actor.organization_id},
    )
    try:
        async with (
            worker_wakeups(runtime.wakeup, [h.actor.organization_id]) as first,
            worker_wakeups(runtime.wakeup, [h.actor.organization_id]) as second,
        ):
            await first.wait(timeout_seconds=0.001)
            await second.wait(timeout_seconds=0.001)
            waits = [asyncio.create_task(w.wait(timeout_seconds=5)) for w in (first, second)]
            await asyncio.sleep(0)
            result = await enqueue(h, runtime.wakeup)
            async with asyncio.timeout(1):
                await asyncio.gather(*waits)
            claims = await asyncio.gather(
                jobs.run_once(worker_id="worker-a", organization_id=h.actor.organization_id),
                jobs.run_once(worker_id="worker-b", organization_id=h.actor.organization_id),
            )
            assert sorted(claims) == [False, True]
            assert [job.id for job in effects] == [result.job.id]
            with pytest.raises(ValueError, match="TENANT_SCOPE"):
                await jobs.run_once(worker_id="worker-a", organization_id=h.foreign.organization_id)
            count = await h.sql(
                "SELECT count(*) FROM assistant_messages WHERE organization_id=:org "
                "AND dedupe_key='worker-wake-once'",
                {"org": h.actor.organization_id},
            )
            assert count.scalar_one() == 1
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("publish_fails", [False, True])
async def test_accepted_job_replay_never_enqueues_again_when_publication_fails(
    retrieval_harness: RetrievalHarness, publish_fails: bool
):
    h = retrieval_harness
    hints = Hints(publish_fails)
    key = uuid4().hex
    accepted = await enqueue(h, hints, key)
    replay = await enqueue(h, hints, key)
    assert replay.replayed and replay.job.id == accepted.job.id
    if not publish_fails:
        assert len([hint for hint in hints.hints if hint.kind == "job.ready"]) == 1
    count = await h.sql(
        "SELECT count(*) FROM assistant_jobs WHERE organization_id=:org AND turn_id=:turn",
        {"org": h.actor.organization_id, "turn": accepted.turn.id},
    )
    assert count.scalar_one() == 1
