"""Web context is temporary across every copy, including historical transcript blocks."""

# pyright: reportPrivateUsage=false
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.database import create_session_factory
from app.modules.feedback.adapters.payload_repository import RetentionTransactions
from app.modules.feedback.application.retention_service import RetentionService
from app.modules.retrieval.adapters.mock import MockRetrievalAdapter
from app.modules.retrieval.domain.models import PublicSearchRequest, SearchBatch
from tests.retrieval_fixtures import RetrievalHarness, pytestmark, retrieval_harness
from tests.test_retrieval_replay_integration import _executor_parts, _request

__all__ = ["pytestmark", "retrieval_harness"]
MARKER = "web-retention-source-marker"


class MarkerProvider(MockRetrievalAdapter):
    async def search(self, request: PublicSearchRequest, *, timeout_seconds: float) -> SearchBatch:
        batch = await super().search(request, timeout_seconds=timeout_seconds)
        source = replace(
            batch.sources[0],
            title=MARKER,
            text=MARKER,
            fingerprint=hashlib.sha256(MARKER.encode()).hexdigest(),
        )
        return replace(batch, sources=(source,))


async def source_and_block(h: RetrievalHarness):
    from app.modules.assistant.adapters.retrieval_projection import RetrievalBlockProjector
    from work_management_ai.tools.web.contracts import WebSearchOutput

    tools, _ = _executor_parts(h, provider=MarkerProvider())
    result = await tools.execute(_request(h))
    assert result.status == "SUCCEEDED", result.safe_error_code
    source = WebSearchOutput.model_validate(result.typed_output).evidence[0]
    block = dict(
        kind="retrieval_answer",
        schema_version="1.0",
        context_run_id=str(h.run_id),
        outcome="ANSWER_READY",
        claims=[dict(text=MARKER, evidence_ids=[str(source.evidence_id)])],
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
        limitations=[],
        observed_at=source.observed_at,
        availability="AVAILABLE",
    )
    projector = RetrievalBlockProjector(h.service())
    stored = await projector.prepare_for_storage(h.actor, block)
    return source, stored


async def purge_source_only(h: RetrievalHarness):
    async with RetentionTransactions(create_session_factory(h.engine))(
        h.actor.organization_id
    ) as repo:
        locators = await repo.claim_expired(
            h.actor.organization_id, datetime.now(UTC) + timedelta(days=31), 100
        )
        locator = next(
            item for item in locators if item.storage_kind == "retrieval_source_snapshots"
        )
        await repo.purge(locator, locator.fence)
        await repo.evidence(1, datetime.now(UTC))


@pytest.mark.asyncio
async def test_web_inventory_purge_preserves_history_and_accounting(
    retrieval_harness: RetrievalHarness,
):
    from app.modules.assistant.adapters.retrieval_projection import RetrievalBlockProjector
    from app.modules.feedback.domain.retention import PAYLOAD_FIELDS

    h = retrieval_harness
    source, block = await source_and_block(h)
    # Include an unrelated business card in the same persisted message.
    business = dict(
        kind="proposal", workflow_run_id=str(uuid4()), proposal_id=str(uuid4()), proposal_version=1
    )
    message_id = uuid4()
    await h.sql(
        "INSERT INTO assistant_messages(id,organization_id,conversation_id,"
        "sequence,role,turn_id,content_blocks) "
        "VALUES (:id,:org,:conversation,2,'ASSISTANT',:turn,CAST(:blocks AS jsonb))",
        dict(
            id=message_id,
            org=h.actor.organization_id,
            conversation=h.job.conversation_id,
            turn=h.job.turn_id,
            blocks=json.dumps([block, business]),
        ),
    )
    raw = json.dumps({"raw": MARKER})
    await h.sql(
        "UPDATE agent_runs SET typed_output=CAST(:raw AS jsonb),status='COMPLETED' WHERE id=:id",
        dict(id=h.run_id, raw=raw),
    )
    await h.sql(
        "INSERT INTO agent_checkpoints(id,organization_id,orchestration_run_id,"
        "agent_run_id,sequence,node,typed_state,checkpoint_version) "
        "VALUES (:id,:org,:run,:agent,1,'retrieve',CAST(:raw AS jsonb),'1')",
        dict(
            id=uuid4(), org=h.actor.organization_id, run=h.orchestration_id, agent=h.run_id, raw=raw
        ),
    )
    before = (
        await h.sql(
            "SELECT reserved FROM retrieval_run_budgets WHERE agent_run_id=:id", {"id": h.run_id}
        )
    ).scalar_one()
    audit_before = (
        await h.sql(
            "SELECT count(*) FROM audit_events WHERE organization_id=:org",
            {"org": h.actor.organization_id},
        )
    ).scalar_one()
    service = RetentionService(RetentionTransactions(create_session_factory(h.engine)))
    now = datetime.now(UTC) + timedelta(days=31)
    assert (
        await service.purge_once(organization_id=h.foreign.organization_id, now=now)
    ).purged == 0
    kinds = set(
        (
            await h.sql(
                "SELECT storage_kind FROM ai_retention_payloads WHERE organization_id=:org",
                {"org": h.actor.organization_id},
            )
        ).scalars()
    )
    assert {"assistant_messages", "retrieval_source_snapshots"} <= kinds
    assert (await service.purge_once(organization_id=h.actor.organization_id, now=now)).purged > 0
    for table, fields in PAYLOAD_FIELDS.items():
        copies = (
            await h.sql(
                f"SELECT {','.join('s.' + field for field in fields)} FROM {table} s "
                "JOIN ai_retention_payloads p ON p.organization_id=s.organization_id "
                f"AND p.owner_id=s.id AND p.storage_kind='{table}' WHERE s.organization_id=:org",
                {"org": h.actor.organization_id},
            )
        ).all()
        assert MARKER not in str(copies), table
        assert "public planning" not in str(copies), table
    snapshot = (
        await h.sql(
            "SELECT payload,canonical_url,fingerprint FROM retrieval_source_snapshots WHERE id=:id",
            {"id": source.evidence_id},
        )
    ).one()
    assert snapshot.payload == {}
    assert snapshot.canonical_url == f"urn:expired:{source.evidence_id}"
    assert snapshot.fingerprint == source.fingerprint
    blocks = (
        await h.sql(
            "SELECT content_blocks FROM assistant_messages WHERE id=:id", {"id": message_id}
        )
    ).scalar_one()
    assert blocks[1] == business
    assert blocks[0]["availability"] == "EXPIRED"
    assert not blocks[0]["claims"] and not blocks[0]["sources"]
    assert (await RetrievalBlockProjector(h.service()).project(h.actor, block))[
        "availability"
    ] == "EXPIRED"
    assert (
        await h.sql(
            "SELECT reserved FROM retrieval_run_budgets WHERE agent_run_id=:id", {"id": h.run_id}
        )
    ).scalar_one() == before
    assert (
        await h.sql(
            "SELECT count(*) FROM audit_events WHERE organization_id=:org",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() >= audit_before
    assert (await service.purge_once(organization_id=h.actor.organization_id, now=now)).purged == 0


@pytest.mark.asyncio
async def test_loaded_public_block_cannot_publish_after_source_purge(
    retrieval_harness: RetrievalHarness,
):
    from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory

    h = retrieval_harness
    _, stored = await source_and_block(h)
    await h.sql(
        "UPDATE assistant_conversations SET last_message_sequence=1 WHERE id=:id",
        {"id": h.job.conversation_id},
    )
    await purge_source_only(h)
    # Simulate the exact race after Python projection but before public INSERT.
    with pytest.raises((ValueError, DBAPIError), match="CONTEXT_EXPIRED"):
        async with PostgreSQLAssistantTransactionFactory(create_session_factory(h.engine))(
            h.actor
        ) as tx:
            await tx.repository.append_assistant_blocks(
                job=h.job, blocks=(stored,), dedupe_key="expired-public"
            )
            await tx.commit()


@pytest.mark.asyncio
async def test_snapshot_purge_role_has_only_expired_scrub_permission(
    retrieval_harness: RetrievalHarness,
):
    h = retrieval_harness
    source, _ = await source_and_block(h)
    async with h.engine.connect() as connection, connection.begin():
        await connection.execute(text("SET LOCAL ROLE app_retention"))
        await connection.execute(
            text("SELECT set_config('app.organization_id',:org,true)"),
            {"org": str(h.actor.organization_id)},
        )
        for statement in (
            "UPDATE retrieval_source_snapshots SET payload='{}' WHERE id=:id",
            "UPDATE retrieval_source_snapshots SET fingerprint=repeat('b',64) WHERE id=:id",
            "DELETE FROM retrieval_source_snapshots WHERE id=:id",
        ):
            with pytest.raises(DBAPIError):
                async with connection.begin_nested():
                    await connection.execute(text(statement), {"id": source.evidence_id})
    await purge_source_only(h)
    tools, _ = _executor_parts(h, provider=MarkerProvider())
    replay = await tools.execute(_request(h))
    assert replay.status == "REJECTED"
    assert not replay.typed_output and not replay.evidence


@pytest.mark.asyncio
async def test_downstream_model_return_rechecks_purged_web_source(
    retrieval_harness: RetrievalHarness,
):
    import asyncio

    from pydantic import BaseModel

    from app.modules.planning_runs.adapters.ai_runtime import WorkflowRecordingModelGateway
    from app.modules.planning_runs.adapters.transaction import (
        PostgreSQLPlanningRunTransactionFactory,
    )
    from work_management_ai.model_gateway.contracts import (
        StructuredModelRequest,
        StructuredModelResponse,
    )

    h = retrieval_harness
    source, _ = await source_and_block(h)
    run_id = uuid4()
    await h.sql(
        "INSERT INTO workflow_runs(id,organization_id,requested_by_membership_id,status,"
        "workflow_name,workflow_version,verifier_version,input_goal_text) "
        "VALUES (:id,:org,:member,'RUNNING','planning','1','1','synthetic planning')",
        dict(id=run_id, org=h.actor.organization_id, member=h.actor.membership_id),
    )
    started, release = asyncio.Event(), asyncio.Event()

    class Output(BaseModel):
        answer: str

    class Provider:
        calls = 0

        async def generate_structured[T: BaseModel](
            self, request: StructuredModelRequest[T]
        ) -> StructuredModelResponse[T]:
            self.calls += 1
            started.set()
            await release.wait()
            return StructuredModelResponse(
                parsed=request.output_schema.model_validate({"answer": MARKER}),
                model_ref="mock:retention",
            )

    async def guard():
        return await h.service().load_evidence(
            h.actor, (source.evidence_id,), expected_hashes=(source.fingerprint,)
        )

    provider = Provider()
    gateway = WorkflowRecordingModelGateway(
        gateway=provider,
        transaction_factory=PostgreSQLPlanningRunTransactionFactory(
            create_session_factory(h.engine)
        ),
        organization_id=h.actor.organization_id,
        workflow_run_id=run_id,
        context_guard=guard,
    )
    request = StructuredModelRequest(
        invocation_key="retention.test", messages=(), output_schema=Output, timeout_seconds=5
    )
    operation = asyncio.create_task(gateway.generate_structured(request))
    try:
        await asyncio.wait_for(started.wait(), 5)
        await purge_source_only(h)
    finally:
        release.set()
    with pytest.raises(ValueError, match="CONTEXT_EXPIRED"):
        await operation
    with pytest.raises(ValueError, match="CONTEXT_EXPIRED"):
        await gateway.generate_structured(request)
    assert provider.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("representation", ["structured", "legacy"])
async def test_downstream_checkpoint_inherits_source_expiry_and_cannot_resurrect_it(
    retrieval_harness: RetrievalHarness,
    representation: str,
):
    h = retrieval_harness
    source, _ = await source_and_block(h)
    workflow, checkpoint = uuid4(), uuid4()
    await h.sql(
        "INSERT INTO workflow_runs(id,organization_id,requested_by_membership_id,status,"
        "workflow_name,workflow_version,verifier_version,input_goal_text) "
        "VALUES (:id,:org,:member,'RUNNING','planning','1','1','synthetic planning')",
        dict(id=workflow, org=h.actor.organization_id, member=h.actor.membership_id),
    )
    state = json.dumps(
        dict(
            untrusted_web_evidence=[
                dict(
                    evidence_id=str(source.evidence_id),
                    fingerprint=source.fingerprint,
                    excerpt=MARKER,
                )
            ]
        )
    )
    if representation == "legacy":
        state = json.dumps(
            dict(
                context_reference_ids=[f"web:{source.evidence_id}:{source.fingerprint}"],
                proposal=dict(description=MARKER),
            )
        )

    async def save(identity: UUID, sequence: int):
        async with h.engine.connect() as connection, connection.begin():
            await connection.execute(text("SET LOCAL ROLE app_runtime"))
            await connection.execute(
                text("SELECT set_config('app.organization_id',:org,true)"),
                {"org": str(h.actor.organization_id)},
            )
            await connection.execute(
                text(
                    "INSERT INTO workflow_checkpoints(id,organization_id,workflow_run_id,"
                    "node,sequence,state) "
                    "VALUES (:id,:org,:workflow,'verify',:sequence,CAST(:state AS jsonb))"
                ),
                dict(
                    id=identity,
                    org=h.actor.organization_id,
                    workflow=workflow,
                    sequence=sequence,
                    state=state,
                ),
            )

    await save(checkpoint, 1)
    deadline = (
        await h.sql(
            "SELECT expires_at FROM ai_retention_payloads WHERE owner_id=:id", {"id": checkpoint}
        )
    ).scalar_one()
    assert deadline <= source.expires_at
    await purge_source_only(h)
    with pytest.raises(DBAPIError, match="CONTEXT_EXPIRED"):
        await save(uuid4(), 2)


@pytest.mark.asyncio
async def test_loaded_tool_output_cannot_be_committed_after_source_purge(
    retrieval_harness: RetrievalHarness,
):
    from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory

    h = retrieval_harness
    source, _ = await source_and_block(h)
    # A second invocation is in flight with a previously loaded, valid source.
    invocation = uuid4()
    await h.sql(
        "INSERT INTO tool_invocations(id,organization_id,agent_run_id,tool_id,tool_version,"
        "risk_level,idempotency_key,typed_input,context_references,status,dedupe_key) "
        "VALUES (:id,:org,:run,'web.search','1.0.0','LOW',:key,'{}','[]','RUNNING',:key)",
        dict(id=invocation, org=h.actor.organization_id, run=h.run_id, key=str(invocation)),
    )
    await purge_source_only(h)
    with pytest.raises(DBAPIError, match="CONTEXT_EXPIRED"):
        async with PostgreSQLAssistantTransactionFactory(create_session_factory(h.engine))(
            h.actor
        ) as tx:
            await tx.repository.finish_tool_invocation(
                organization_id=h.actor.organization_id,
                invocation_id=invocation,
                status="SUCCEEDED",
                typed_output=dict(evidence=[source.model_dump(mode="json")]),
                context_references=(),
                safe_error_code=None,
            )
            await tx.commit()


@pytest.mark.asyncio
async def test_actual_planning_loader_rejects_a_purged_agent_binding(
    retrieval_harness: RetrievalHarness,
):
    from app.modules.assistant.adapters.retrieval_planning import PlanningRetrievalContext
    from app.modules.organization.domain.roles import MembershipRole

    h = retrieval_harness
    await h.sql("UPDATE memberships SET role='MANAGER' WHERE id=:id", {"id": h.actor.membership_id})
    h.actor = replace(h.actor, role=MembershipRole.MANAGER)
    source, _ = await source_and_block(h)
    workflow, agent, handoff = uuid4(), uuid4(), uuid4()
    await h.sql(
        "INSERT INTO workflow_runs(id,organization_id,requested_by_membership_id,status,"
        "workflow_name,workflow_version,verifier_version,input_goal_text) "
        "VALUES (:id,:org,:member,'RUNNING','planning','1','1','synthetic planning')",
        dict(id=workflow, org=h.actor.organization_id, member=h.actor.membership_id),
    )
    parent = (
        await h.sql(
            "SELECT id FROM agent_runs WHERE orchestration_run_id=:run AND agent_id='orchestrator'",
            {"run": h.orchestration_id},
        )
    ).scalar_one()
    refs = json.dumps(
        dict(
            retrieval_context_refs=[
                dict(evidence_id=str(source.evidence_id), fingerprint=source.fingerprint)
            ]
        )
    )
    outer = json.dumps(
        [
            dict(
                resource_id=str(source.evidence_id),
                fingerprint=source.fingerprint,
                version=1,
                organization_id=str(h.actor.organization_id),
                resource_type="WEB_SOURCE_SNAPSHOT",
            )
        ]
    )
    await h.sql(
        "INSERT INTO agent_handoffs(id,organization_id,orchestration_run_id,parent_agent_run_id,"
        "target_agent_id,target_agent_version,capability,objective,typed_input,context_references,"
        "budget,step_id,idempotency_key,dedupe_key) VALUES "
        "(:id,:org,:run,:parent,'planning','1.0.0',"
        "'planning.create','synthetic planning',CAST(:refs AS jsonb),"
        "CAST(:outer AS jsonb),'{}',:key,:key,:key)",
        dict(
            id=handoff,
            org=h.actor.organization_id,
            run=h.orchestration_id,
            parent=parent,
            refs=refs,
            outer=outer,
            key=str(handoff),
        ),
    )
    await h.sql(
        "INSERT INTO agent_runs(id,organization_id,orchestration_run_id,parent_agent_run_id,"
        "inbound_handoff_id,agent_id,agent_version,manifest_fingerprint,capability,typed_input,budget,"
        "status,workflow_run_id) VALUES (:id,:org,:run,:parent,:handoff,'planning','1.0.0','test',"
        "'planning.create',CAST(:refs AS jsonb),'{}','RUNNING',:workflow)",
        dict(
            id=agent,
            org=h.actor.organization_id,
            run=h.orchestration_id,
            parent=parent,
            handoff=handoff,
            refs=refs,
            workflow=workflow,
        ),
    )
    context = PlanningRetrievalContext(
        sessions=create_session_factory(h.engine), budgets=h.service()
    )
    assert MARKER in str(await context.load(h.actor, workflow))
    async with RetentionTransactions(create_session_factory(h.engine))(
        h.actor.organization_id
    ) as repo:
        locators = await repo.claim_expired(
            h.actor.organization_id, datetime.now(UTC) + timedelta(days=31), 100
        )
        locator = next(
            item
            for item in locators
            if item.storage_kind == "agent_runs" and item.owner_id == agent
        )
        await repo.purge(locator, locator.fence)
    assert (
        await h.sql("SELECT typed_input FROM agent_runs WHERE id=:id", {"id": agent})
    ).scalar_one() == {}
    with pytest.raises(ValueError, match="CONTEXT_EXPIRED"):
        await context.load(h.actor, workflow)
