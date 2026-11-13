"""After-commit wake-up and live SSE authority on real PostgreSQL/Redis."""

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.database import create_session_factory
from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory
from app.modules.assistant.application.event_service import AssistantEventService
from app.modules.identity.adapters.auth_repository import SqlAlchemyAuthTransactionFactory
from app.modules.identity.application.current_actor_service import CurrentActorService
from app.modules.optimization.adapters.redis_runtime import create_redis_runtime
from app.modules.optimization.application.wakeup import PollingSubscription, WakeupHint, WakeupScope
from tests.retrieval_fixtures import RetrievalHarness, pytestmark, retrieval_harness

__all__ = ["pytestmark", "retrieval_harness"]


class Hints:
    def __init__(self, fail: bool = False):
        self.hints: list[WakeupHint] = []
        self.fail = fail

    async def publish(self, hint: WakeupHint) -> None:
        if self.fail:
            raise RuntimeError("synthetic publish failure")
        self.hints.append(hint)

    @asynccontextmanager
    async def subscribe(self, scope: WakeupScope):
        yield PollingSubscription()


async def append(transaction: Any, h: RetrievalHarness, marker: str):
    await transaction.repository.append_assistant_blocks(
        job=h.job, blocks=({"kind": "text", "text": marker},), dedupe_key=marker
    )


@pytest.mark.asyncio
async def test_event_hints_published_only_after_successful_commit(
    retrieval_harness: RetrievalHarness,
):
    h = retrieval_harness
    hints = Hints()
    transactions = PostgreSQLAssistantTransactionFactory(
        create_session_factory(h.engine), wakeup=hints
    )
    async with transactions(h.actor) as transaction:
        await append(transaction, h, "commit-wakeup")
        assert hints.hints == []
        await transaction.commit()
        assert len(hints.hints) == 1
    assert hints.hints[0].scope == WakeupScope(
        h.actor.organization_id, "assistant.events", h.job.conversation_id
    )


@pytest.mark.asyncio
async def test_rollback_does_not_publish_event_hint(retrieval_harness: RetrievalHarness):
    h = retrieval_harness
    hints = Hints()
    transactions = PostgreSQLAssistantTransactionFactory(
        create_session_factory(h.engine), wakeup=hints
    )
    with pytest.raises(ValueError, match="rollback"):
        async with transactions(h.actor) as transaction:
            await append(transaction, h, "rollback-wakeup")
            raise ValueError("rollback")
    assert hints.hints == []
    rows = await h.sql(
        "SELECT count(*) FROM assistant_messages "
        "WHERE organization_id=:org AND dedupe_key='rollback-wakeup'",
        {"org": h.actor.organization_id},
    )
    assert rows.scalar_one() == 0


@pytest.mark.asyncio
async def test_publish_failure_cannot_change_committed_event(retrieval_harness: RetrievalHarness):
    h = retrieval_harness
    transactions = PostgreSQLAssistantTransactionFactory(
        create_session_factory(h.engine), wakeup=Hints(True)
    )
    async with transactions(h.actor) as transaction:
        await append(transaction, h, "failed-hint")
        await transaction.commit()
    rows = await h.sql(
        "SELECT count(*) FROM assistant_messages "
        "WHERE organization_id=:org AND dedupe_key='failed-hint'",
        {"org": h.actor.organization_id},
    )
    assert rows.scalar_one() == 1


@pytest.mark.asyncio
async def test_revoked_membership_blocks_each_source_read(retrieval_harness: RetrievalHarness):
    h = retrieval_harness
    transactions = PostgreSQLAssistantTransactionFactory(create_session_factory(h.engine))
    async with transactions(h.actor) as transaction:
        assert (
            await transaction.repository.get_conversation_snapshot(
                actor=h.actor, conversation_id=h.job.conversation_id
            )
            is not None
        )
    await h.sql(
        "UPDATE memberships SET is_active=false WHERE id=:id", {"id": h.actor.membership_id}
    )
    async with transactions(h.actor) as transaction:
        assert (
            await transaction.repository.get_conversation_snapshot(
                actor=h.actor, conversation_id=h.job.conversation_id
            )
            is None
        )
        assert (
            await transaction.repository.list_events(
                actor=h.actor, conversation_id=h.job.conversation_id, after_sequence=0
            )
            == []
        )


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("RUN_REDIS_INTEGRATION") != "1", reason="requires owned Redis")
async def test_committed_hint_wakes_sse_without_driving_cursor_or_creating_work(
    retrieval_harness: RetrievalHarness,
):
    h = retrieval_harness
    runtime = create_redis_runtime(
        Settings(
            environment="test",
            redis_wakeup_enabled=True,
            redis_url=SecretStr(os.environ["REDIS_TEST_URL"]),
            redis_namespace="sse-" + uuid4().hex[:20],
        )
    )
    sessions = create_session_factory(h.engine)
    transactions = PostgreSQLAssistantTransactionFactory(sessions, wakeup=runtime.wakeup)
    actors = CurrentActorService(SqlAlchemyAuthTransactionFactory(sessions))
    service = AssistantEventService(
        transaction_factory=transactions, wakeup=runtime.wakeup, actors=actors
    )
    frames = await service.replay(
        actor=h.actor, conversation_id=h.job.conversation_id, after_sequence=0
    )
    cursor = max([int(frame.splitlines()[0][4:]) for frame in frames] or [0])
    stream = service.stream(
        actor=h.actor, conversation_id=h.job.conversation_id, after_sequence=cursor
    )
    try:
        assert (await anext(stream)).startswith(": heartbeat")
        async with transactions(h.actor) as transaction:
            await append(transaction, h, "live-wakeup")
            await transaction.commit()
        async with asyncio.timeout(1):
            frame = await anext(stream)
        assert frame.startswith(f"id: {cursor + 1}\n")
        assert "RESPONSE_AVAILABLE" in frame
        rows = await h.sql(
            "SELECT count(*) FROM assistant_jobs WHERE organization_id=:org",
            {"org": h.actor.organization_id},
        )
        assert rows.scalar_one() == 1
    finally:
        await stream.aclose()
        await runtime.aclose()


@pytest.mark.asyncio
async def test_savepoint_commit_never_publishes_before_root_transaction(
    retrieval_harness: RetrievalHarness,
):
    h = retrieval_harness
    hints = Hints()
    transactions = PostgreSQLAssistantTransactionFactory(
        create_session_factory(h.engine), wakeup=hints
    )
    async with transactions(h.actor) as transaction:
        await append(transaction, h, "outer-event")
        async with transaction.session.begin_nested():
            await append(transaction, h, "nested-event")
        assert hints.hints == []
        await transaction.commit()
    assert len(hints.hints) == 1 and hints.hints[0].sequence == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["membership", "user"])
async def test_live_stream_closes_after_authority_is_revoked(
    retrieval_harness: RetrievalHarness, target: str
):
    from tests.test_sse_wakeup import Waker

    h = retrieval_harness
    sessions = create_session_factory(h.engine)
    service = AssistantEventService(
        transaction_factory=PostgreSQLAssistantTransactionFactory(sessions),
        actors=CurrentActorService(SqlAlchemyAuthTransactionFactory(sessions)),
        wakeup=Waker(),
    )
    stream = service.stream(actor=h.actor, conversation_id=h.job.conversation_id, after_sequence=0)
    assert (await anext(stream)).startswith(": heartbeat")
    if target == "membership":
        await h.sql(
            "UPDATE memberships SET is_active=false WHERE id=:id", {"id": h.actor.membership_id}
        )
    else:
        await h.sql("UPDATE users SET is_active=false WHERE id=:id", {"id": h.actor.user_id})
    assert [frame async for frame in stream] == []


@pytest.mark.asyncio
async def test_workflow_stream_and_source_deny_a_downgraded_manager(
    retrieval_harness: RetrievalHarness,
):
    from dataclasses import replace

    from app.modules.organization.domain.roles import MembershipRole
    from app.modules.planning_runs.adapters.transaction import (
        PostgreSQLPlanningRunTransactionFactory,
    )
    from app.modules.planning_runs.application.event_service import WorkflowEventService
    from tests.test_planning_run_api_integration import run_for
    from tests.test_sse_wakeup import Waker

    h = retrieval_harness
    await h.sql("UPDATE memberships SET role='MANAGER' WHERE id=:id", {"id": h.actor.membership_id})
    manager = replace(h.actor, role=MembershipRole.MANAGER)
    sessions = create_session_factory(h.engine)
    transactions = PostgreSQLPlanningRunTransactionFactory(sessions)
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    run = replace(run_for(manager), created_at=now, updated_at=now)
    async with transactions(manager) as tx:
        await tx.repository.create_workflow_run(run=run)
    service = WorkflowEventService(
        transaction_factory=transactions,
        actors=CurrentActorService(SqlAlchemyAuthTransactionFactory(sessions)),
        wakeup=Waker(),
    )
    stream = service.stream(actor=manager, run_id=run.id, after_sequence=0)
    assert (await anext(stream)).startswith(": heartbeat")
    await h.sql(
        "UPDATE memberships SET role='EMPLOYEE' WHERE id=:id", {"id": manager.membership_id}
    )
    async with transactions(manager) as tx:
        await tx.repository.append_event(
            actor=manager,
            run_id=run.id,
            event_type="workflow.progress.v1",
            public_payload={"phase": "private"},
        )
    assert [frame async for frame in stream] == []
    async with transactions(manager) as tx:
        assert await tx.repository.list_events(actor=manager, run_id=run.id, after_sequence=0) == []
