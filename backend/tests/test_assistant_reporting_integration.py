# pyright: reportUnusedImport=false
# ruff: noqa: F811
"""Real SQL report chat: immutable drafts, read-only status and safe resolution."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.assistant.adapters.reporting_tools import ReportChatService
from app.modules.work.adapters.project_repository import SqlAlchemyProjectTransactionFactory
from app.modules.work.application.project_service import ProjectService
from tests.test_report_api_integration import (
    ReportHarness,
    report_harness,  # noqa: F401
)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("report_harness")
async def test_prepare_report_returns_exact_card(report_harness: ReportHarness):
    h = report_harness
    service = ReportChatService(
        reports=h.service,
        projects=ProjectService(
            SqlAlchemyProjectTransactionFactory(
                async_sessionmaker(h.engine, expire_on_commit=False)
            )
        ),
    )
    result = await service.prepare(
        actor=h.actor,
        intent={
            "operation": "PREPARE_REPORT",
            "project_reference": str(h.project_id),
            "kind": "WEEKLY",
            "relative_period": "PREVIOUS",
            "locale": "en",
        },
        key=str(uuid4()),
    )
    card = result["card"]
    assert card["kind"] == "report"
    stored = await h.service.get(actor=h.actor, report_id=UUID(card["report_id"]))
    assert str(stored.selected_version.id) == str(card["report_version_id"])
    assert stored.snapshot.snapshot_hash == card["snapshot_hash"]
    assert not stored.publications
    assert stored.snapshot.period.local_start.weekday() == 0
    assert len(card["metrics"]) <= 6
    assert len(card["sources"]) <= 8


@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_question_is_read_only(report_harness: ReportHarness):
    h = report_harness
    service = ReportChatService(
        reports=h.service,
        projects=ProjectService(
            SqlAlchemyProjectTransactionFactory(
                async_sessionmaker(h.engine, expire_on_commit=False)
            )
        ),
    )
    result = await service.prepare(
        actor=h.actor,
        intent={
            "operation": "EXPLAIN_STATUS",
            "project_reference": str(h.project_id),
            "locale": "vi",
        },
        key=str(uuid4()),
    )
    assert result["context"]["snapshot"]["metrics"]["tasks.status.total_count"]["value"] == "1"
    assert (
        await h.sql(
            "SELECT count(*) FROM reports WHERE organization_id=:org",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 0
    assert (
        await h.sql(
            "SELECT count(*) FROM report_publications WHERE organization_id=:org",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ambiguous_project_requires_selection(report_harness: ReportHarness):
    h = report_harness
    service = ReportChatService(
        reports=h.service,
        projects=ProjectService(
            SqlAlchemyProjectTransactionFactory(
                async_sessionmaker(h.engine, expire_on_commit=False)
            )
        ),
    )
    await h.sql(
        "INSERT INTO projects(id,organization_id,name,created_by_membership_id,"
        "updated_by_membership_id) SELECT :id,organization_id,name,created_by_membership_id,"
        "updated_by_membership_id FROM projects WHERE id=:project",
        {"id": uuid4(), "project": h.project_id},
    )
    name = (
        await h.sql("SELECT name FROM projects WHERE id=:id", {"id": h.project_id})
    ).scalar_one()
    result = await service.prepare(
        actor=h.actor,
        intent={"operation": "PREPARE_REPORT", "project_reference": name, "locale": "en"},
        key=str(uuid4()),
    )
    assert result["resolution"] == "AMBIGUOUS"
    assert len(result["candidates"]) == 2
    missing = await service.prepare(
        actor=h.actor,
        intent={"operation": "PREPARE_REPORT", "project_reference": str(uuid4()), "locale": "en"},
        key=str(uuid4()),
    )
    foreign = await service.prepare(
        actor=h.foreign,
        intent={
            "operation": "PREPARE_REPORT",
            "project_reference": str(h.project_id),
            "locale": "en",
        },
        key=str(uuid4()),
    )
    assert missing == foreign


@pytest.mark.integration
@pytest.mark.asyncio
async def test_chat_hub_card_history_and_source_revoke(report_harness: ReportHarness):
    from httpx import ASGITransport, AsyncClient

    from app.core.config import Settings
    from app.modules.assistant.adapters.agent_runtime import (
        AssistantTurnExecutor,
        InactivePlanningToolExecutor,
        build_agent_registry,
        build_execution_engine_factory,
    )
    from app.modules.assistant.adapters.report_projection import ReportStatusContexts
    from app.modules.assistant.adapters.reporting_tools import (
        ChatReportingToolAdapter,
        ReportBlockProjector,
    )
    from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory
    from app.modules.assistant.adapters.work_tools import RecordingToolExecutor
    from app.modules.assistant.application.job_service import AssistantJobService
    from app.modules.identity.adapters.auth_repository import SqlAlchemyAuthTransactionFactory
    from app.modules.identity.application.current_actor_service import CurrentActorService
    from app.modules.planning_runs.adapters.ai_runtime import build_model_gateway

    h = report_harness
    sessions = async_sessionmaker(h.engine, expire_on_commit=False)
    registry, tools = build_agent_registry()
    actors = CurrentActorService(SqlAlchemyAuthTransactionFactory(sessions))
    transactions = PostgreSQLAssistantTransactionFactory(sessions)
    contexts = ReportStatusContexts(sessions, "UTC")
    chat = ReportChatService(
        reports=h.service, projects=ProjectService(SqlAlchemyProjectTransactionFactory(sessions))
    )
    reporting = RecordingToolExecutor(
        transaction_factory=transactions,
        tool_registry=tools,
        backend=ChatReportingToolAdapter(actors=actors, service=chat, contexts=contexts),
    )
    projector = ReportBlockProjector(reports=h.service, contexts=contexts)
    from app.modules.assistant.adapters.report_chat_usage import (
        ReportChatUsage,
        ReportChatUsageFactory,
    )
    from work_management_ai.runtime.contracts import AgentHandoff

    usages: list[ReportChatUsage] = []

    class UsageFactory(ReportChatUsageFactory):
        def __call__(self, handoff: AgentHandoff) -> ReportChatUsage:
            value = super().__call__(handoff)
            usages.append(value)
            return value

    usage_factory = UsageFactory(sessions)
    executor = AssistantTurnExecutor(
        transaction_factory=transactions,
        registry=registry,
        engine_factory=build_execution_engine_factory(
            model_gateway=build_model_gateway(Settings(environment="test", ai_provider="mock")),
            registry=registry,
            actor_resolver=actors,
            work_tool_executor=InactivePlanningToolExecutor(),
            transaction_factory=transactions,
            reporting_tool_executor=reporting,
            reporting_usage_factory=usage_factory,
        ),
        block_projector=projector,
    )

    from app.modules.assistant.domain.models import AssistantJob

    async def handle(*, job: AssistantJob, worker_id: str):
        await executor.execute_job(job=job, actor=h.actor)

    jobs = AssistantJobService(
        transaction_factory=transactions,
        handler=handle,
        organization_scopes={h.actor.organization_id},
    )
    app = h.app()
    app.state.assistant_service.block_projector = projector
    app.state.assistant_event_service.block_projector = projector
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        conversation = await client.post(
            "/api/v1/ai/conversations",
            json={"locale": "en"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert conversation.status_code == 201, conversation.text
        cid = conversation.json()["id"]
        message = await client.post(
            f"/api/v1/ai/conversations/{cid}/messages",
            json={"locale": "en", "message": f"Project status for {h.project_id}?"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert message.status_code == 202, message.text
        assert await jobs.run_once(
            worker_id="report-chat-test", organization_id=h.actor.organization_id
        )
        snapshot = await client.get(f"/api/v1/ai/conversations/{cid}")
        blocks = [b for m in snapshot.json()["messages"] for b in m["content_blocks"]]
        card = next((b for b in blocks if b["kind"] == "project_status"), None)
        assert card is not None, blocks
        assert card["analysis_state"] == "VERIFIED"
        assert (
            await h.sql(
                "SELECT count(*) FROM reports WHERE organization_id=:org",
                {"org": h.actor.organization_id},
            )
        ).scalar_one() == 0
        forged = {**card, "analysis": ["Invented private claim"]}
        assert (await projector.project(h.actor, forged))["kind"] == "safe_error"
        assert (await projector.project(h.foreign, card))["kind"] == "safe_error"
        # A restarted initial tool call must reuse the immutable operational snapshot.
        from work_management_ai.runtime.contracts import ActorReference, ToolExecutionRequest

        replay = await ChatReportingToolAdapter(
            actors=actors, service=chat, contexts=contexts
        ).execute(
            ToolExecutionRequest(
                agent_run_id=UUID(card["context_run_id"]),
                tool_id="reporting.chat",
                tool_version="1.0.0",
                call_id="restart",
                actor=ActorReference(
                    organization_id=h.actor.organization_id, membership_id=h.actor.membership_id
                ),
                typed_input={
                    "operation": "EXPLAIN_STATUS",
                    "project_reference": str(h.project_id),
                    "locale": "en",
                },
                idempotency_key="restart",
            )
        )
        assert replay.status == "SUCCEEDED"
        replay_card = replay.typed_output["card"]
        assert isinstance(replay_card, dict)
        assert replay_card["snapshot_id"] == card["snapshot_id"]
        prepared = await client.post(
            f"/api/v1/ai/conversations/{cid}/messages",
            json={"locale": "en", "message": f"Generate a report for project {h.project_id}"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert prepared.status_code == 202
        assert await jobs.run_once(
            worker_id="report-chat-test", organization_id=h.actor.organization_id
        )
        history = await client.get(f"/api/v1/ai/conversations/{cid}")
        report_card = next(
            b
            for m in history.json()["messages"]
            for b in m["content_blocks"]
            if b["kind"] == "report"
        )
        assert (await projector.project(h.actor, report_card))["kind"] == "report"
        for changes in [
            {"project_label": "Forged secret"},
            {"report_version_id": str(uuid4())},
            {"generation_state": "PUBLISHED"},
            {"href": "https://evil.test"},
        ]:
            assert (await projector.project(h.actor, {**report_card, **changes}))[
                "kind"
            ] == "safe_error"
        assert (await projector.project(h.foreign, report_card))["kind"] == "safe_error"
        # Reserve-before-call survives a simulated interruption and claim transfer.
        from dataclasses import replace

        usage = usages[0]
        await h.sql(
            "UPDATE assistant_jobs SET status='RUNNING',locked_by=:worker,"
            "lease_until=now()+interval '60 seconds' WHERE id=:id",
            {"id": usage.job.id, "worker": usage.job.locked_by},
        )
        await h.sql("UPDATE agent_runs SET status='RUNNING' WHERE id=:id", {"id": usage.run_id})
        fresh = ReportChatUsage(sessions, usage.job, usage.handoff)
        assert (await fresh.load()).attempts == 2
        assert await fresh.reserve(input_tokens=100, output_tokens=100) == 3
        restarted = ReportChatUsage(sessions, usage.job, usage.handoff)
        assert (await restarted.load()).attempts == 3
        with pytest.raises(ValueError, match="MODEL_BUDGET"):
            await restarted.reserve(input_tokens=100, output_tokens=100)
        await restarted.consume_retry()
        with pytest.raises(ValueError, match="RETRY_BUDGET"):
            await restarted.consume_retry()
        stale = ReportChatUsage(
            sessions, replace(usage.job, attempt_count=usage.job.attempt_count + 1), usage.handoff
        )
        with pytest.raises(ValueError, match="STALE_CLAIM"):
            await stale.load()
        from app.modules.assistant.adapters.execution_recorder import PostgreSQLExecutionRecorder
        from work_management_ai.runtime.contracts import AgentId, AgentResult, AgentRunStatus

        old_recorder = PostgreSQLExecutionRecorder(
            transaction_factory=transactions,
            registry=registry,
            job=usage.job,
            actor=h.actor,
            block_projector=projector,
        )
        await h.sql(
            "UPDATE assistant_jobs SET attempt_count=attempt_count+1,"
            "locked_by='successor' WHERE id=:id",
            {"id": usage.job.id},
        )
        with pytest.raises(RuntimeError, match="CLAIM"):
            await old_recorder.finish_agent_run(
                usage.run_id,
                AgentResult(
                    agent_id=AgentId.REPORTING,
                    agent_version="1.0.0",
                    status=AgentRunStatus.FAILED,
                    safe_error_code="STALE",
                    typed_output={},
                    stop_reason="STALE",
                ),
            )
        assert (
            await h.sql("SELECT status FROM agent_runs WHERE id=:id", {"id": usage.run_id})
        ).scalar_one() == "RUNNING"
        await h.sql(
            "UPDATE assistant_jobs SET status='COMPLETED' WHERE id=:id", {"id": usage.job.id}
        )
        await h.sql("UPDATE agent_runs SET status='COMPLETED' WHERE id=:id", {"id": usage.run_id})
        await h.sql(
            "INSERT INTO projects(id,organization_id,name,created_by_membership_id,"
            "updated_by_membership_id) "
            "SELECT :id,organization_id,name,created_by_membership_id,updated_by_membership_id "
            "FROM projects WHERE id=:project",
            {"id": uuid4(), "project": h.project_id},
        )
        name = (
            await h.sql("SELECT name FROM projects WHERE id=:id", {"id": h.project_id})
        ).scalar_one()
        ambiguous = await client.post(
            f"/api/v1/ai/conversations/{cid}/messages",
            json={"locale": "en", "message": f'Generate a report for project "{name}"'},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert ambiguous.status_code == 202
        assert await jobs.run_once(
            worker_id="report-chat-test", organization_id=h.actor.organization_id
        )
        history = await client.get(f"/api/v1/ai/conversations/{cid}")
        question = next(
            b
            for m in history.json()["messages"]
            for b in m["content_blocks"]
            if b["kind"] == "question"
        )
        assert len(question["response_context"]["candidates"]) == 2
        await h.sql(
            "UPDATE memberships SET role='EMPLOYEE' WHERE id=:id", {"id": h.actor.membership_id}
        )
        question = {
            "kind": "question",
            "question": "Choose project",
            "response_context": {
                "reporting": True,
                "candidates": [{"id": str(h.project_id), "name": "Private project"}],
            },
        }
        assert (await projector.project(h.actor, question))["kind"] == "safe_error"
        frames = await app.state.assistant_event_service.replay(
            actor=h.actor, conversation_id=UUID(cid), after_sequence=0
        )
        import json

        for frame in frames:
            payload = json.loads(frame.split("data: ", 1)[1].strip())
            assert '"kind":"project_status"' not in json.dumps(payload, separators=(",", ":"))
            assert '"kind":"report"' not in json.dumps(payload, separators=(",", ":"))
            assert '"reporting":true' not in json.dumps(payload, separators=(",", ":"))
        revoked = await client.get(f"/api/v1/ai/conversations/{cid}")
        assert all(
            b["kind"] not in {"project_status", "report"}
            and not b.get("response_context", {}).get("reporting")
            for m in revoked.json()["messages"]
            for b in m["content_blocks"]
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exact_version_deep_link_does_not_select_current_draft(report_harness: ReportHarness):
    from httpx import ASGITransport, AsyncClient

    h = report_harness
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        created = await client.post(
            "/api/v1/reports",
            json={**h.body(), "narrative_enabled": False},
            headers={"Idempotency-Key": str(uuid4())},
        )
        result = created.json()
        rid = result["report"]["id"]
        vid = result["selected_version"]["id"]
        later = uuid4()
        await h.sql(
            "INSERT INTO report_versions(id,organization_id,report_id,snapshot_id,origin,locale,"
            "created_at,payload) SELECT :id,organization_id,report_id,snapshot_id,origin,locale,"
            "created_at,payload FROM report_versions WHERE id=:version",
            {"id": later, "version": UUID(vid)},
        )
        await h.sql(
            "UPDATE reports SET selected_version_id=:version,version=version+1 WHERE id=:id",
            {"version": later, "id": UUID(rid)},
        )
        exact = await client.get(f"/api/v1/reports/{rid}?version_id={vid}")
        assert exact.status_code == 200, exact.text
        assert exact.json()["selected_version"]["id"] == vid
        assert exact.json()["report"]["selected_version_id"] == str(later)
        assert (await client.get(f"/api/v1/reports/{rid}?version_id={uuid4()}")).status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lost_report_claim_does_not_fail_same_worker_successor(report_harness: ReportHarness):
    from httpx import ASGITransport, AsyncClient

    from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory
    from app.modules.assistant.application.job_service import AssistantJobService
    from app.modules.assistant.domain.models import AssistantJob, AssistantJobClaimLost

    h = report_harness
    sessions = async_sessionmaker(h.engine, expire_on_commit=False)
    claimed: list[AssistantJob] = []

    async def handler(*, job: AssistantJob, worker_id: str):
        claimed.append(job)
        await h.sql(
            "UPDATE assistant_jobs SET attempt_count=attempt_count+1 WHERE id=:id", {"id": job.id}
        )
        raise AssistantJobClaimLost("ASSISTANT_JOB_CLAIM_LOST")

    jobs = AssistantJobService(
        transaction_factory=PostgreSQLAssistantTransactionFactory(sessions),
        handler=handler,
        organization_scopes={h.actor.organization_id},
    )
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        conversation = await client.post(
            "/api/v1/ai/conversations",
            json={"locale": "en"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        cid = conversation.json()["id"]
        submitted = await client.post(
            f"/api/v1/ai/conversations/{cid}/messages",
            json={"locale": "en", "message": f"Project status for {h.project_id}"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert submitted.status_code == 202
        assert await jobs.run_once(worker_id="same-worker", organization_id=h.actor.organization_id)
        row = (
            await h.sql(
                "SELECT status,attempt_count,locked_by FROM assistant_jobs WHERE id=:id",
                {"id": claimed[0].id},
            )
        ).one()
        assert tuple(row) == ("RUNNING", 2, "same-worker")
