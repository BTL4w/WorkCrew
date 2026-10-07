"""Real expiry, purge, tenant boundary and business-history preservation."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.test_report_api_integration import ReportHarness, pytestmark, report_harness
from tests.test_report_trigger_integration import inputs

__all__ = ["pytestmark", "report_harness"]


@pytest.mark.asyncio
async def test_purge_is_tenant_scoped_retry_safe_and_preserves_business(
    report_harness: ReportHarness,
):
    from uuid import uuid4

    from app.modules.feedback.adapters.payload_repository import RetentionTransactions
    from app.modules.feedback.application.retention_service import RetentionService
    from app.modules.reporting.domain.commands import CreateReportCommand, PublishReportCommand

    h = report_harness
    triggers, trigger = await inputs(h)
    run = await triggers.ensure(actor=h.actor, trigger=trigger)
    before = (
        await h.sql(
            "SELECT payload FROM report_versions WHERE organization_id=:org ORDER BY id",
            {"org": h.actor.organization_id},
        )
    ).all()
    service = RetentionService(
        RetentionTransactions(async_sessionmaker(h.engine, expire_on_commit=False))
    )
    now = datetime.now(UTC)
    assert (
        await service.purge_once(
            organization_id=h.foreign.organization_id, now=now + timedelta(days=31)
        )
    ).purged == 0
    assert (await service.purge_once(organization_id=h.actor.organization_id, now=now)).purged == 0
    result = await service.purge_once(
        organization_id=h.actor.organization_id, now=now + timedelta(days=31)
    )
    assert result.purged >= 1
    assert (
        await h.sql(
            "SELECT execution_plan,checkpoint FROM orchestration_runs WHERE id=:id", {"id": run.id}
        )
    ).one() == ({}, {})
    assert (
        await service.purge_once(
            organization_id=h.actor.organization_id, now=now + timedelta(days=31)
        )
    ).purged == 0
    assert (
        await h.sql(
            "SELECT payload FROM report_versions WHERE organization_id=:org ORDER BY id",
            {"org": h.actor.organization_id},
        )
    ).all() == before
    manual = await h.service.create(
        actor=h.actor,
        command=CreateReportCommand(project_id=h.project_id, narrative_enabled=False),
        idempotency_key=str(uuid4()),
    )
    published = await h.service.publish_metrics(
        actor=h.actor,
        report_id=manual.report.id,
        command=PublishReportCommand(
            mode="METRICS_ONLY",
            report_version_id=manual.selected_version.id,
            snapshot_hash=manual.snapshot.snapshot_hash,
        ),
        expected_version=manual.report.version,
        idempotency_key=str(uuid4()),
    )
    assert published.publications[0].report_version_id == manual.selected_version.id


@pytest.mark.asyncio
async def test_every_classified_copy_is_purged(report_harness: ReportHarness):
    """Real source columns, including terminal/retired immutable copies, all scrubbed."""
    from uuid import uuid4

    from sqlalchemy import insert

    from app.modules.assistant.adapters import database_models as assistant
    from app.modules.feedback.adapters.payload_repository import RetentionTransactions
    from app.modules.feedback.application.retention_service import RetentionService
    from app.modules.feedback.domain.retention import PAYLOAD_FIELDS
    from app.modules.planning_runs.adapters import database_models as planning
    from app.modules.progress.adapters import evidence_models, extraction_models
    from tests.test_evaluation_curation_integration import admin_service, command
    from tests.test_feedback_outcome_integration import reviewed

    h = report_harness
    _, feedback_id = await reviewed(h)
    curation = await admin_service(h)
    candidate = await curation.prepare(
        actor=h.actor, feedback_id=feedback_id, idempotency_key=str(uuid4())
    )
    triggers, trigger = await inputs(h)
    run = await triggers.ensure(actor=h.actor, trigger=trigger)
    now = datetime.now(UTC)
    org = h.actor.organization_id
    member = h.actor.membership_id
    conversation, message, turn, agent, workflow, evidence, extraction = [uuid4() for _ in range(7)]
    raw = {"private": "expired-runtime-copy"}
    rows = [
        (
            assistant.AssistantConversationModel,
            dict(id=conversation, owner_membership_id=member, locale="en"),
        ),
        (
            assistant.AssistantMessageModel,
            dict(
                id=message,
                conversation_id=conversation,
                sequence=1,
                role="USER",
                content_blocks=[{"type": "text", "text": "retained transcript"}],
                created_by_membership_id=member,
            ),
        ),
        (
            assistant.AssistantTurnModel,
            dict(
                id=turn,
                conversation_id=conversation,
                user_message_id=message,
                actor_membership_id=member,
                objective="raw execution",
                locale="en",
            ),
        ),
        (
            assistant.AgentRunModel,
            dict(
                id=agent,
                orchestration_run_id=run.id,
                agent_id="orchestrator",
                agent_version="1",
                manifest_fingerprint="test",
                capability="test",
                typed_input=raw,
                typed_output=raw,
                budget={},
                status="COMPLETED",
            ),
        ),
        (
            assistant.AgentHandoffModel,
            dict(
                id=uuid4(),
                orchestration_run_id=run.id,
                parent_agent_run_id=agent,
                target_agent_id="planning",
                target_agent_version="1",
                capability="test",
                objective="raw execution",
                typed_input=raw,
                context_references=[raw],
                budget={},
                step_id="test",
                idempotency_key=str(uuid4()),
                dedupe_key=str(uuid4()),
            ),
        ),
        (
            assistant.AgentCheckpointModel,
            dict(
                id=uuid4(),
                orchestration_run_id=run.id,
                agent_run_id=agent,
                sequence=1,
                node="final",
                typed_state=raw,
                checkpoint_version="1",
            ),
        ),
        (
            assistant.SkillInvocationModel,
            dict(
                id=uuid4(),
                agent_run_id=agent,
                skill_id="test",
                skill_version="1",
                typed_input=raw,
                typed_output=raw,
                status="SUCCEEDED",
                dedupe_key=str(uuid4()),
            ),
        ),
        (
            assistant.ToolInvocationModel,
            dict(
                id=uuid4(),
                agent_run_id=agent,
                tool_id="test",
                tool_version="1",
                risk_level="LOW",
                idempotency_key=str(uuid4()),
                typed_input=raw,
                typed_output=raw,
                context_references=[raw],
                status="SUCCEEDED",
                dedupe_key=str(uuid4()),
            ),
        ),
        (
            assistant.AssistantEventModel,
            dict(
                id=uuid4(),
                conversation_id=conversation,
                orchestration_run_id=run.id,
                sequence=1,
                event_type="test",
                public_payload=raw,
            ),
        ),
        (
            assistant.AssistantJobModel,
            dict(
                id=uuid4(),
                conversation_id=conversation,
                turn_id=turn,
                orchestration_run_id=run.id,
                requester_membership_id=member,
                payload=raw,
            ),
        ),
        (
            planning.WorkflowRunModel,
            dict(
                id=workflow,
                project_id=h.project_id,
                requested_by_membership_id=member,
                workflow_name="planning",
                workflow_version="1",
                verifier_version="1",
                input_goal_text="raw execution",
                error_message="raw diagnostic",
                status="WAITING_FOR_DECISION",
            ),
        ),
        (
            planning.WorkflowCheckpointModel,
            dict(
                id=uuid4(),
                workflow_run_id=workflow,
                node="await_manager_decision",
                sequence=1,
                state=raw,
            ),
        ),
        (
            planning.WorkflowEventModel,
            dict(
                id=uuid4(),
                workflow_run_id=workflow,
                sequence=1,
                event_type="test",
                public_payload=raw,
            ),
        ),
        (
            planning.WorkflowJobModel,
            dict(
                id=uuid4(),
                workflow_run_id=workflow,
                job_type="planning",
                payload=raw,
                last_error="raw diagnostic",
            ),
        ),
        (
            planning.ContextReferenceModel,
            dict(
                id=uuid4(),
                workflow_run_id=workflow,
                resource_type="project",
                resource_id=h.project_id,
                provenance_notes="raw notes",
            ),
        ),
        (
            evidence_models.EvidenceOriginalModel,
            dict(
                id=evidence,
                uploader_membership_id=member,
                version=1,
                filename="original.txt",
                idempotency_key=str(uuid4()),
                storage_key=f"{org}/{evidence}",
                state="READY",
                sha256="a" * 64,
                byte_length=1,
                mime_type="text/plain",
                uploaded_at=now,
                expires_at=now + timedelta(days=100),
                lease_id=uuid4(),
                lease_until=now,
                confirmed_at=now,
            ),
        ),
        (
            extraction_models.EvidenceProcessingJobModel,
            dict(
                id=extraction,
                evidence_id=evidence,
                version=1,
                uploader_membership_id=member,
                revision=1,
                idempotency_key=str(uuid4()),
                state="READY",
                lease_until=now,
                lease_attempts=0,
                model_attempts=0,
                input_tokens=0,
                output_tokens=0,
                next_visual=0,
                result=raw,
                created_at=now,
            ),
        ),
        (
            extraction_models.EvidenceSegmentModel,
            dict(
                id=uuid4(),
                job_id=extraction,
                evidence_id=evidence,
                version=1,
                source_id=uuid4(),
                content=raw,
            ),
        ),
    ]
    async with h.engine.begin() as conn:
        for model, values in rows:
            await conn.execute(insert(model).values(organization_id=org, **values))
    business_tables = (
        "report_versions",
        "report_metric_snapshots",
        "report_publications",
        "report_review_decisions",
        "feedback",
        "assistant_messages",
        "evidence_originals",
    )
    before = {
        table: (
            await h.sql(
                f"SELECT to_jsonb(t) FROM {table} t WHERE organization_id=:org ORDER BY id",
                {"org": org},
            )
        ).all()
        for table in business_tables
    }
    registry = (
        (
            await h.sql(
                "SELECT storage_kind FROM ai_retention_payloads WHERE organization_id=:org",
                {"org": org},
            )
        )
        .scalars()
        .all()
    )
    assert set(registry) == set(PAYLOAD_FIELDS)
    service = RetentionService(
        RetentionTransactions(async_sessionmaker(h.engine, expire_on_commit=False))
    )
    result = await service.purge_once(organization_id=org, now=now + timedelta(days=31))
    assert result.purged == len(registry)
    for table, fields in PAYLOAD_FIELDS.items():
        actual = (
            await h.sql(
                f"SELECT {','.join(fields)} FROM {table} WHERE organization_id=:org", {"org": org}
            )
        ).all()
        expected = dict(fields)
        if table == "workflow_jobs":
            expected["last_error"] = "CONTEXT_EXPIRED"
        assert actual and all(tuple(row) == tuple(expected.values()) for row in actual), table
    for table, values in before.items():
        assert (
            await h.sql(
                f"SELECT to_jsonb(t) FROM {table} t WHERE organization_id=:org ORDER BY id",
                {"org": org},
            )
        ).all() == values
    assert (
        await h.sql(
            "SELECT node,state FROM workflow_checkpoints WHERE workflow_run_id=:id",
            {"id": workflow},
        )
    ).one() == ("await_manager_decision", {})
    from app.modules.reporting.domain.reports import ReportError

    with pytest.raises(ReportError, match="CONTEXT_EXPIRED"):
        await curation.curate(
            actor=h.actor,
            command=command(candidate),
            expected_version=1,
            idempotency_key=str(uuid4()),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["APPROVE", "REJECT", "EDIT"])
async def test_expired_ai_context_preserves_manual_proposal_paths(
    report_harness: ReportHarness, operation: str
):
    from uuid import UUID, uuid4

    from httpx import ASGITransport, AsyncClient

    from app.core.config import Settings
    from app.modules.feedback.adapters.payload_repository import RetentionTransactions
    from app.modules.feedback.application.retention_service import RetentionService
    from app.modules.identity.domain.auth import AuthenticatedActor
    from app.modules.planning_runs.adapters.ai_runtime import build_planning_job_handlers
    from app.modules.planning_runs.adapters.transaction import (
        PostgreSQLPlanningRunTransactionFactory,
    )
    from app.modules.planning_runs.application.job_service import JobService

    h = report_harness
    sessions = async_sessionmaker(h.engine, expire_on_commit=False)
    factory = PostgreSQLPlanningRunTransactionFactory(sessions)

    class Actors:
        async def resolve(
            self, *, organization_id: UUID, membership_id: UUID
        ) -> AuthenticatedActor | None:
            return (
                h.actor
                if (organization_id, membership_id)
                == (h.actor.organization_id, h.actor.membership_id)
                else None
            )

    jobs = JobService(
        transaction_factory=factory,
        handlers=build_planning_job_handlers(
            Settings(environment="test", ai_provider="mock"), factory, Actors()
        ),
        organization_scopes={h.actor.organization_id},
    )
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        created = await client.post(
            "/api/v1/ai/planning-runs",
            json={"message": "Plan a customer conference", "locale": "en"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert created.status_code == 202, created.text
        run = UUID(created.json()["run_id"])
        assert await jobs.run_once("retention-test", h.actor.organization_id)
        snapshot = await client.get(f"/api/v1/workflow-runs/{run}")
        assert snapshot.json()["status"] == "WAITING_FOR_DECISION", snapshot.text
        proposal = snapshot.json()["current_proposal"]
        await RetentionService(RetentionTransactions(sessions)).purge_once(
            organization_id=h.actor.organization_id, now=datetime.now(UTC) + timedelta(days=31)
        )
        historical = await client.get(f"/api/v1/workflow-runs/{run}")
        assert historical.status_code == 200, historical.text
        assert historical.json()["current_proposal"] == proposal
        if operation == "EDIT":
            version = await client.get(f"/api/v1/proposals/{proposal['proposal_id']}/versions/1")
            result = await client.patch(
                f"/api/v1/proposals/{proposal['proposal_id']}",
                json={"content": version.json()["content"]},
                headers={"Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
            )
        else:
            result = await client.post(
                f"/api/v1/approvals/{proposal['approval_id']}/decision",
                json={"decision": operation, "reason": "Manual reviewed decision"},
                headers={"Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
            )
        assert result.status_code in (200, 202), result.text
        assert await jobs.run_once("retention-test", h.actor.organization_id)
        final = await client.get(f"/api/v1/workflow-runs/{run}")
        assert final.status_code == 200, final.text
        assert final.json()["status"] == (
            "WAITING_FOR_DECISION" if operation == "EDIT" else "COMPLETED"
        ), final.text


@pytest.mark.asyncio
async def test_inclusive_expiry_boundary_and_live_role_safeguards(report_harness: ReportHarness):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.modules.feedback.adapters.payload_repository import RetentionTransactions
    from app.modules.feedback.application.retention_service import RetentionService

    h = report_harness
    triggers, trigger = await inputs(h)
    run = await triggers.ensure(actor=h.actor, trigger=trigger)
    sessions = async_sessionmaker(h.engine, expire_on_commit=False)
    transactions = RetentionTransactions(sessions)
    deadline = (
        await h.sql(
            "SELECT expires_at FROM ai_retention_payloads WHERE organization_id=:org "
            "AND storage_kind='orchestration_runs' AND owner_id=:id",
            {"org": h.actor.organization_id, "id": run.id},
        )
    ).scalar_one()
    async with transactions(h.foreign.organization_id) as repo:
        assert (
            await repo.session.scalar(
                text("SELECT count(*) FROM ai_retention_payloads WHERE owner_id=:id"),
                {"id": run.id},
            )
            == 0
        )
    with pytest.raises(DBAPIError, match="invalid retention scrub"):
        async with transactions(h.actor.organization_id) as repo:
            await repo.session.execute(
                text(
                    "UPDATE orchestration_runs SET execution_plan='{}',checkpoint='{}' WHERE id=:id"
                ),
                {"id": run.id},
            )
    assert (
        await RetentionService(transactions).purge_once(
            organization_id=h.actor.organization_id, now=deadline - timedelta(microseconds=1)
        )
    ).purged == 0
    assert (
        await RetentionService(transactions).purge_once(
            organization_id=h.actor.organization_id, now=deadline
        )
    ).purged == 1
    assert (
        await h.sql(
            "SELECT rolbypassrls OR rolsuper OR rolcanlogin FROM pg_roles "
            "WHERE rolname='app_retention'"
        )
    ).scalar_one() is False


@pytest.mark.asyncio
async def test_shorter_policy_blocks_database_writes_before_cleanup(report_harness: ReportHarness):
    from uuid import uuid4

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory

    h = report_harness
    triggers, trigger = await inputs(h)
    run = await triggers.ensure(actor=h.actor, trigger=trigger)
    factory = PostgreSQLAssistantTransactionFactory(
        async_sessionmaker(h.engine, expire_on_commit=False)
    )
    with pytest.raises(DBAPIError, match="CONTEXT_EXPIRED"):
        async with factory(h.actor) as tx:
            await tx.session.execute(
                text("SELECT set_config('app.ai_raw_retention_days','0',true)")
            )
            await tx.session.execute(
                text(
                    'UPDATE orchestration_runs SET checkpoint=\'{"private":"stale"}\' WHERE id=:id'
                ),
                {"id": run.id},
            )
    with pytest.raises(DBAPIError, match="CONTEXT_EXPIRED"):
        async with factory(h.actor) as tx:
            await tx.session.execute(
                text("SELECT set_config('app.ai_raw_retention_days','0',true)")
            )
            await tx.session.execute(
                text(
                    "INSERT INTO agent_runs(id,organization_id,orchestration_run_id,agent_id,"
                    "agent_version,manifest_fingerprint,capability,typed_input,budget) "
                    "VALUES (:id,:org,:run,'orchestrator','1','test','test','{}','{}')"
                ),
                {"id": uuid4(), "org": h.actor.organization_id, "run": run.id},
            )
    assert (
        await h.sql("SELECT checkpoint FROM orchestration_runs WHERE id=:id", {"id": run.id})
    ).scalar_one() == {}


@pytest.mark.asyncio
async def test_retention_role_cannot_change_live_report_job(report_harness: ReportHarness):
    from uuid import uuid4

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.modules.feedback.adapters.payload_repository import RetentionTransactions
    from app.modules.reporting.domain.commands import CreateReportCommand

    h = report_harness
    report = await h.service.create(
        actor=h.actor,
        command=CreateReportCommand(project_id=h.project_id),
        idempotency_key=str(uuid4()),
    )
    transactions = RetentionTransactions(async_sessionmaker(h.engine, expire_on_commit=False))
    with pytest.raises(DBAPIError, match="invalid retention report transition"):
        async with transactions(h.actor.organization_id) as repo:
            await repo.session.execute(
                text(
                    "UPDATE report_generation_jobs SET state='AI_UNAVAILABLE',"
                    "safe_error_code='CONTEXT_EXPIRED',fence=fence+1,"
                    "lease_until=NULL,lease_owner=NULL WHERE id=:id"
                ),
                {"id": report.generation_id},
            )
    assert (
        await h.sql(
            "SELECT state FROM report_generation_jobs WHERE id=:id", {"id": report.generation_id}
        )
    ).scalar_one() == "QUEUED"


@pytest.mark.asyncio
async def test_manual_pointer_exemption_rejects_raw_values_and_payload_changes(
    report_harness: ReportHarness,
):
    import json
    from uuid import uuid4

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.modules.feedback.adapters.payload_repository import RetentionTransactions
    from app.modules.feedback.application.retention_service import RetentionService
    from app.modules.planning_runs.adapters.transaction import (
        PostgreSQLPlanningRunTransactionFactory,
    )

    h = report_harness
    assert not (
        await h.sql(
            "SELECT retention_business_metadata('workflow_events', "
            '\'{"event_type":"proposal.validating","public_payload":[]}\'::jsonb)'
        )
    ).scalar_one()
    run, job = uuid4(), uuid4()
    await h.sql(
        "INSERT INTO workflow_runs(id,organization_id,requested_by_membership_id,"
        "workflow_name,workflow_version,verifier_version,input_goal_text,status) "
        "VALUES (:id,:org,:member,'planning','1','1','raw','WAITING_FOR_DECISION')",
        {"id": run, "org": h.actor.organization_id, "member": h.actor.membership_id},
    )
    sessions = async_sessionmaker(h.engine, expire_on_commit=False)
    factory = PostgreSQLPlanningRunTransactionFactory(sessions)
    insert_job = (
        "INSERT INTO workflow_jobs(id,organization_id,workflow_run_id,job_type,payload) "
        "VALUES (:id,:org,:run,'proposal.revalidate',CAST(:payload AS jsonb))"
    )
    valid = {"instruction": "REVALIDATE", "proposal_id": str(uuid4()), "proposal_version": 1}
    async with factory(h.actor) as tx:
        await tx.session.execute(
            text(insert_job),
            {"id": job, "org": h.actor.organization_id, "run": run, "payload": json.dumps(valid)},
        )
        await tx.commit()
    assert (
        await h.sql("SELECT count(*) FROM ai_retention_payloads WHERE owner_id=:id", {"id": job})
    ).scalar_one() == 0
    await RetentionService(RetentionTransactions(sessions)).purge_once(
        organization_id=h.actor.organization_id, now=datetime.now(UTC) + timedelta(days=31)
    )
    with pytest.raises(DBAPIError, match="CONTEXT_EXPIRED"):
        async with factory(h.actor) as tx:
            await tx.session.execute(
                text(insert_job),
                {
                    "id": uuid4(),
                    "org": h.actor.organization_id,
                    "run": run,
                    "payload": json.dumps({**valid, "proposal_id": "private prompt material"}),
                },
            )
    with pytest.raises(DBAPIError, match="CONTEXT_EXPIRED"):
        async with factory(h.actor) as tx:
            await tx.session.execute(
                text(
                    insert_job.replace("job_type,payload)", "job_type,payload,last_error)").replace(
                        "CAST(:payload AS jsonb))", "CAST(:payload AS jsonb),'raw diagnostic')"
                    )
                ),
                {
                    "id": uuid4(),
                    "org": h.actor.organization_id,
                    "run": run,
                    "payload": json.dumps(valid),
                },
            )
    for value in ("private context", str(uuid4())):
        with pytest.raises(DBAPIError, match=r"retention metadata|pointers immutable"):
            async with factory(h.actor) as tx:
                await tx.session.execute(
                    text("UPDATE workflow_jobs SET payload=CAST(:payload AS jsonb) WHERE id=:id"),
                    {"id": job, "payload": json.dumps({**valid, "proposal_id": value})},
                )
    with pytest.raises(DBAPIError, match="retention metadata"):
        async with factory(h.actor) as tx:
            await tx.session.execute(
                text("UPDATE workflow_jobs SET last_error='private prompt trace' WHERE id=:id"),
                {"id": job},
            )
