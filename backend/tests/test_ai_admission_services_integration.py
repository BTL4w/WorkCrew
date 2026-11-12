"""Every explicit new-work boundary gates effects after durable replay."""

import os
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.optimization.application.admission import (
    AdmissionDecision,
    AdmissionUnavailable,
    AIAdmissionService,
    RateLimitRule,
    RateLimitUnavailable,
)
from app.modules.reporting.adapters.generation_repository import GenerationTransactions
from app.modules.reporting.application.generation_service import GenerationService
from app.modules.reporting.application.report_service import ReportService
from app.modules.reporting.domain.commands import CreateReportCommand, GenerateNarrativeCommand
from tests.test_daily_update_api_integration import body, confirm, draft, seed_task
from tests.test_evidence_api_integration import Harness, harness
from tests.test_evidence_assessment_integration import assessed_app
from tests.test_report_api_integration import ReportHarness, report_harness
from tests.test_risk_api_integration import setup

__all__ = ["harness", "report_harness"]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


class Unavailable:
    calls = 0

    async def check(self, rules: tuple[RateLimitRule, ...]) -> AdmissionDecision:
        self.calls += 1
        raise RateLimitUnavailable()


def denied():
    return AIAdmissionService(Unavailable(), namespace="test:denied")


@pytest.mark.asyncio
async def test_report_metrics_survive_outage_but_new_narrative_does_not(
    report_harness: ReportHarness,
):
    h = report_harness
    service = ReportService(h.service.transactions, admission=denied())
    command = CreateReportCommand(project_id=h.project_id, narrative_enabled=False)
    metrics = await service.create(actor=h.actor, command=command, idempotency_key=uuid4().hex)
    assert metrics.generation_state == "NOT_REQUESTED"
    with pytest.raises(AdmissionUnavailable):
        await service.create(
            actor=h.actor,
            command=command.model_copy(update={"narrative_enabled": True}),
            idempotency_key=uuid4().hex,
        )
    assert (
        await h.sql(
            "SELECT count(*) FROM report_generation_jobs WHERE organization_id=:org",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 0
    key = uuid4().hex
    accepted = await h.service.create(
        actor=h.actor,
        command=command.model_copy(update={"narrative_enabled": True}),
        idempotency_key=key,
    )
    replay = await service.create(
        actor=h.actor,
        command=command.model_copy(update={"narrative_enabled": True}),
        idempotency_key=key,
    )
    assert replay.replayed and replay.generation_id == accepted.generation_id


@pytest.mark.asyncio
async def test_explicit_report_generation_replays_during_outage(report_harness: ReportHarness):
    h = report_harness
    report = await h.service.create(
        actor=h.actor,
        command=CreateReportCommand(project_id=h.project_id, narrative_enabled=False),
        idempotency_key=uuid4().hex,
    )
    transactions = GenerationTransactions(
        async_sessionmaker(h.engine, expire_on_commit=False), "UTC"
    )
    limited = GenerationService(transactions, admission=denied())
    command = GenerateNarrativeCommand(
        base_version_id=report.selected_version.id, snapshot_hash=report.snapshot.snapshot_hash
    )
    values: dict[str, Any] = dict(
        actor=h.actor,
        report_id=report.report.id,
        command=command,
        expected_version=report.report.version,
    )
    with pytest.raises(AdmissionUnavailable):
        await limited.request(**values, idempotency_key=uuid4().hex)
    key = uuid4().hex
    accepted = await GenerationService(transactions).request(**values, idempotency_key=key)
    replay = await limited.request(**values, idempotency_key=key)
    assert replay.replayed and replay.generation_id == accepted.generation_id
    assert (
        await h.sql(
            "SELECT count(*) FROM report_generation_jobs WHERE report_id=:id",
            {"id": report.report.id},
        )
    ).scalar_one() == 1


@pytest.mark.asyncio
async def test_direct_assessment_denial_keeps_owner_manual_confirmation(harness: Harness):
    from app.modules.progress.application.assessment_service import AssessmentService

    task = await seed_task(harness)
    app = assessed_app(harness)
    original = app.state.assessment_service
    app.state.assessment_service = AssessmentService(original.transactions, admission=denied())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        d = await draft(client, [body(task)])
        result = await client.post(
            f"/api/v1/daily-updates/{d['id']}/assess",
            json={"expected_version": d["version"]},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert result.status_code == 503
        assert result.json()["error"]["code"] == "AI_ADMISSION_UNAVAILABLE"
        assert (await confirm(client, d)).status_code == 201


@pytest.mark.asyncio
async def test_explicit_risk_refresh_denial_and_replay(harness: Harness):
    from app.modules.risk.application.risk_service import RiskService

    task = await seed_task(harness)
    app, original, model, manager = await setup(harness)
    app.state.risk_service = RiskService(original.transactions, model, admission=denied())
    key = uuid4().hex
    accepted = await original.request_refresh(manager, UUID(task), key)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        replay = await client.post(
            "/api/v1/risks/refresh", json={"task_id": task}, headers={"Idempotency-Key": key}
        )
        assert replay.status_code == 202 and replay.json()["id"] == str(accepted.id)
        result = await client.post(
            "/api/v1/risks/refresh",
            json={"task_id": task},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert result.status_code == 503
        assert model.calls == 0


@pytest.mark.asyncio
async def test_assessment_inside_accepted_turn_is_not_admitted_twice(harness: Harness):
    from datetime import UTC, datetime, timedelta

    from app.core.config import Settings
    from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory
    from app.modules.assistant.application.service import AssistantService
    from app.modules.progress.adapters.daily_update_runtime import build_daily_services
    from app.modules.progress.application.assessment_service import AssessmentService
    from tests.test_assistant_service import NullPlanningSnapshot
    from work_management_ai.model_gateway.mock import MockModelGateway

    class CountingLimiter:
        calls = 0

        async def check(self, rules: tuple[RateLimitRule, ...]) -> AdmissionDecision:
            self.calls += 1
            return AdmissionDecision(True, 0)

    task = await seed_task(harness)
    app = assessed_app(harness)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        d = await draft(client, [body(task)])
    sessions = async_sessionmaker(
        harness.connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    transactions = PostgreSQLAssistantTransactionFactory(sessions)
    limiter = CountingLimiter()
    admission = AIAdmissionService(limiter, namespace="test:accepted")
    assistant = AssistantService(
        transaction_factory=transactions,
        planning_snapshot=NullPlanningSnapshot(),
        orchestrator_version="1.0.0",
        orchestrator_fingerprint="test",
        admission=admission,
    )
    conversation = await assistant.create_conversation(
        actor=harness.actor, locale="en", title=None, request_id="test", idempotency_key=uuid4().hex
    )
    accepted = await assistant.post_message(
        actor=harness.actor,
        conversation_id=conversation.conversation.id,
        message="Prepare my update",
        locale="en",
        card_action=None,
        if_match_version=None,
        request_id="test",
        idempotency_key=uuid4().hex,
    )
    async with transactions(harness.actor) as transaction:
        claimed = await transaction.repository.claim_job(
            organization_id=harness.actor.organization_id,
            worker_id="admission-test",
            now=datetime.now(UTC),
            lease_until=datetime.now(UTC) + timedelta(minutes=1),
        )
        assert claimed is not None and claimed.id == accepted.job.id
        await transaction.commit()
    # This is the worker composition, with the same enabled setting as the API.
    _, worker_assessments, _ = build_daily_services(
        sessions=sessions,
        settings=Settings(
            ai_provider="mock",
            ai_admission_enabled=True,
            redis_url=SecretStr("redis://localhost:1/0"),
        ),
        evidence=harness.service,
        gateway=MockModelGateway(fixtures={}),
    )
    ref = await worker_assessments.assess(
        harness.actor, UUID(d["id"]), 1, uuid4().hex, "accepted-tool"
    )
    assert ref.id is not None and limiter.calls == 1
    # A direct assessment remains a separate new-work boundary.
    direct = AssessmentService(worker_assessments.transactions, admission=admission)
    await direct.assess(harness.actor, UUID(d["id"]), 1, uuid4().hex, "direct-http")
    assert limiter.calls == 2
