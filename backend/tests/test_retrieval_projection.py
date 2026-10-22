"""Real snapshot projection for transcript/replay; no provider I/O during hydration."""

# pyright: reportPrivateUsage=false
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from tests.retrieval_fixtures import RetrievalHarness, pytestmark, retrieval_harness
from tests.test_retrieval_replay_integration import _executor_parts, _request

__all__ = ["pytestmark", "retrieval_harness"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["valid", "foreign", "peer", "disabled", "expired", "hash", "revoked"]
)
async def test_public_projection_stores_refs_and_rehydrates_current_sources(
    retrieval_harness: RetrievalHarness, reason: str
):
    from app.modules.assistant.adapters.retrieval_projection import RetrievalBlockProjector

    h = retrieval_harness
    tools, _ = _executor_parts(h)
    result = await tools.execute(_request(h))
    from work_management_ai.tools.web.contracts import WebSearchOutput

    source = WebSearchOutput.model_validate(result.typed_output).evidence[0].model_dump(mode="json")
    block = dict(
        kind="retrieval_answer",
        schema_version="1.0",
        context_run_id=str(h.run_id),
        outcome="EVIDENCE_READY",
        claims=[],
        sources=[
            {
                key: source[key]
                for key in (
                    "evidence_id",
                    "title",
                    "canonical_url",
                    "method",
                    "excerpt",
                    "published_at",
                    "source_fetched_at",
                )
            }
        ],
        limitations=["SYNTHESIS_UNAVAILABLE"],
        observed_at=source["observed_at"],
        availability="AVAILABLE",
    )
    projector = RetrievalBlockProjector(h.service())
    stored = (await projector.prepare_for_storage(h.actor, [block]))[0]
    assert "sources" not in stored
    assert stored["source_refs"][0]["fingerprint"] == source["fingerprint"]
    assert source["excerpt"] not in str(stored)
    actor = h.foreign if reason == "foreign" else h.peer if reason == "peer" else h.actor
    if reason == "disabled":
        h.enabled = False
    elif reason == "hash":
        stored["source_refs"][0]["fingerprint"] = "b" * 64
    elif reason == "revoked":
        await h.sql(
            "UPDATE assistant_conversations SET owner_membership_id=:peer WHERE id=:id",
            {"peer": h.peer.membership_id, "id": h.job.conversation_id},
        )
    elif reason == "expired":
        from app.core.database import create_session_factory
        from app.modules.feedback.adapters.payload_repository import RetentionTransactions
        from app.modules.feedback.application.retention_service import RetentionService

        await RetentionService(RetentionTransactions(create_session_factory(h.engine))).purge_once(
            organization_id=h.actor.organization_id, now=datetime.now(UTC) + timedelta(days=31)
        )
    projected = (await projector.project(actor, {"content_blocks": [stored]}))["content_blocks"][0]
    if reason == "valid":
        assert projected["sources"][0]["excerpt"] == source["excerpt"]
        assert projected["sources"][0]["source_fetched_at"] is None
    else:
        assert not projected["sources"] and not projected["claims"]
        assert source["evidence_id"] not in str(projected)
        assert source["excerpt"] not in str(projected)
        assert projected["availability"] == ("EXPIRED" if reason == "expired" else "UNAVAILABLE")


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [False, True])
async def test_recorder_persists_only_refs_and_stopped_job_prevents_publication(
    retrieval_harness: RetrievalHarness, stopped: bool
):
    from app.core.database import create_session_factory
    from app.modules.assistant.adapters.agent_runtime import build_agent_registry
    from app.modules.assistant.adapters.execution_recorder import PostgreSQLExecutionRecorder
    from app.modules.assistant.adapters.retrieval_projection import RetrievalBlockProjector
    from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory
    from work_management_ai.runtime.contracts import RetrievalAnswerResponseBlock
    from work_management_ai.tools.web.contracts import WebSearchOutput

    h = retrieval_harness
    tools, _ = _executor_parts(h)
    result = await tools.execute(_request(h))
    source = WebSearchOutput.model_validate(result.typed_output).evidence[0]
    block = RetrievalAnswerResponseBlock.model_validate(
        dict(
            context_run_id=h.run_id,
            outcome="ANSWER_READY",
            claims=[dict(text="Synthetic verified guidance", evidence_ids=[source.evidence_id])],
            sources=[
                {
                    key: getattr(source, key)
                    for key in (
                        "evidence_id",
                        "title",
                        "canonical_url",
                        "method",
                        "excerpt",
                        "published_at",
                        "source_fetched_at",
                    )
                }
            ],
            observed_at=source.observed_at,
        )
    )
    registry, _ = build_agent_registry()
    projector = RetrievalBlockProjector(h.service())
    recorder = PostgreSQLExecutionRecorder(
        transaction_factory=PostgreSQLAssistantTransactionFactory(create_session_factory(h.engine)),
        registry=registry,
        job=h.job,
        actor=h.actor,
        block_projector=projector,
    )
    await h.sql(
        "UPDATE assistant_conversations SET last_message_sequence=1 WHERE id=:id",
        {"id": h.job.conversation_id},
    )
    if stopped:
        await h.sql("UPDATE assistant_jobs SET status='FAILED' WHERE id=:id", {"id": h.job_id})
        from app.modules.assistant.domain.models import AssistantJobClaimLost

        with pytest.raises(AssistantJobClaimLost):
            await recorder.append_public_blocks(
                h.job.conversation_id, h.job.turn_id, (block,), "task6-test"
            )
        assert (
            await h.sql(
                "SELECT count(*) FROM assistant_messages WHERE conversation_id=:id "
                "AND role='ASSISTANT'",
                {"id": h.job.conversation_id},
            )
        ).scalar_one() == 0
        return
    await recorder.append_public_blocks(
        h.job.conversation_id, h.job.turn_id, (block,), "task6-test"
    )
    stored = (
        await h.sql(
            "SELECT content_blocks FROM assistant_messages WHERE conversation_id=:id "
            "AND role='ASSISTANT'",
            {"id": h.job.conversation_id},
        )
    ).scalar_one()[0]
    assert "sources" not in stored
    assert source.excerpt not in str(stored)
    assert stored["source_refs"][0]["fingerprint"] == source.fingerprint
    before = (
        await h.sql(
            "SELECT count(*) FROM retrieval_attempt_reservations WHERE organization_id=:org",
            {"org": h.actor.organization_id},
        )
    ).scalar_one()
    for _ in range(2):
        hydrated = await projector.project(h.actor, stored)
        assert hydrated["sources"][0]["excerpt"] == source.excerpt
    assert (
        await h.sql(
            "SELECT count(*) FROM retrieval_attempt_reservations WHERE organization_id=:org",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == before

    # Use the real transcript and SSE application services over persisted ref-only blocks.
    from app.modules.assistant.api.schemas import MessageResponse
    from app.modules.assistant.application.event_service import AssistantEventService
    from app.modules.assistant.application.service import AssistantService, ResourceNotFoundError

    transactions = PostgreSQLAssistantTransactionFactory(create_session_factory(h.engine))
    assistant = AssistantService(
        transaction_factory=transactions,
        planning_snapshot=cast(Any, None),
        block_projector=projector,
        orchestrator_version="1.0.0",
        orchestrator_fingerprint="a" * 64,
    )
    events = AssistantEventService(transaction_factory=transactions, block_projector=projector)
    for actor in (h.foreign, h.peer):
        with pytest.raises(ResourceNotFoundError):
            await assistant.get_conversation(actor=actor, conversation_id=h.job.conversation_id)
        with pytest.raises(ResourceNotFoundError):
            await events.authorize(actor=actor, conversation_id=h.job.conversation_id)
        assert (
            await events.replay(
                actor=actor, conversation_id=h.job.conversation_id, after_sequence=0
            )
            == []
        )
    snapshot = await assistant.get_conversation(
        actor=h.actor, conversation_id=h.job.conversation_id
    )
    public = [
        MessageResponse.from_domain(message).model_dump(mode="json")
        for message in snapshot.messages
    ]
    assert source.excerpt in str(public) and "source_refs" not in str(public)
    frames = await events.replay(
        actor=h.actor, conversation_id=h.job.conversation_id, after_sequence=0
    )
    # Existing v1 events carry only message pointers; the client re-fetches the guarded snapshot.
    assert frames and "assistant.turn.response.v1" in str(frames)
    assert source.excerpt not in str(frames) and "source_refs" not in str(frames)
    assert "RESPONSE_AVAILABLE" in str(frames)
    h.enabled = False
    snapshot = await assistant.get_conversation(
        actor=h.actor, conversation_id=h.job.conversation_id
    )
    frames = await events.replay(
        actor=h.actor, conversation_id=h.job.conversation_id, after_sequence=0
    )
    assert "UNAVAILABLE" in str(snapshot.messages)
    for value in (snapshot.messages, frames):
        assert source.excerpt not in str(value)
        assert str(source.evidence_id) not in str(value)
        assert "Synthetic verified guidance" not in str(value)


@pytest.mark.asyncio
async def test_failed_retrieval_publishes_only_safe_outage_for_its_owner(
    retrieval_harness: RetrievalHarness,
):
    from app.modules.assistant.adapters.retrieval_projection import RetrievalBlockProjector
    from tests.retrieval_public_fixtures import public_answer

    h = retrieval_harness
    await h.sql("UPDATE agent_runs SET status='FAILED' WHERE id=:id", {"id": h.run_id})
    block = public_answer()
    block.update(context_run_id=h.run_id, outcome="SOURCE_UNAVAILABLE", claims=[], sources=[])
    projector = RetrievalBlockProjector(h.service())
    stored = await projector.prepare_for_storage(h.actor, block)
    public = await projector.project(h.actor, stored)
    assert public["outcome"] == "SOURCE_UNAVAILABLE"
    assert public["availability"] == "UNAVAILABLE"
    assert not public["sources"] and not public["claims"]
    for actor in (h.foreign, h.peer):
        with pytest.raises(ValueError):
            await projector.prepare_for_storage(actor, block)
