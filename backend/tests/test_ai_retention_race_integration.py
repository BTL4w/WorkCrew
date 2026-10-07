"""A worker holding loaded context cannot persist after purge."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.test_report_api_integration import ReportHarness, pytestmark, report_harness
from tests.test_report_trigger_integration import inputs

__all__ = ["pytestmark", "report_harness"]


@pytest.mark.asyncio
async def test_loaded_worker_cannot_write_after_purge(report_harness: ReportHarness):
    from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory
    from app.modules.feedback.adapters.payload_repository import RetentionTransactions
    from app.modules.feedback.application.retention_service import RetentionService

    h = report_harness
    triggers, trigger = await inputs(h)
    run = await triggers.ensure(actor=h.actor, trigger=trigger)
    sessions = async_sessionmaker(h.engine, expire_on_commit=False)
    factory = PostgreSQLAssistantTransactionFactory(sessions)
    async with factory(h.actor) as tx:
        assert (
            await tx.repository.load_orchestration_checkpoint(
                organization_id=h.actor.organization_id, orchestration_run_id=run.id
            )
            == {}
        )
    await RetentionService(RetentionTransactions(sessions)).purge_once(
        organization_id=h.actor.organization_id, now=datetime.now(UTC) + timedelta(days=31)
    )
    with pytest.raises(ValueError, match="CONTEXT_EXPIRED"):
        async with factory(h.actor) as tx:
            await tx.repository.save_orchestration_checkpoint(
                organization_id=h.actor.organization_id,
                orchestration_run_id=run.id,
                checkpoint={"stale": "loaded"},
                execution_plan={},
            )
    with pytest.raises(ValueError, match="CONTEXT_EXPIRED"):
        async with factory(h.actor) as tx:
            await tx.repository.load_orchestration_checkpoint(
                organization_id=h.actor.organization_id, orchestration_run_id=run.id
            )


@pytest.mark.asyncio
async def test_planning_provider_return_and_retry_reject_expired_context(
    report_harness: ReportHarness,
):
    import asyncio
    from uuid import uuid4

    from pydantic import BaseModel

    from app.modules.feedback.adapters.payload_repository import RetentionTransactions
    from app.modules.feedback.application.retention_service import RetentionService
    from app.modules.planning_runs.adapters.ai_runtime import WorkflowRecordingModelGateway
    from app.modules.planning_runs.adapters.transaction import (
        PostgreSQLPlanningRunTransactionFactory,
    )
    from work_management_ai.model_gateway.contracts import (
        StructuredModelRequest,
        StructuredModelResponse,
    )

    h = report_harness
    run = uuid4()
    await h.sql(
        "INSERT INTO workflow_runs(id,organization_id,project_id,requested_by_membership_id,"
        "status,workflow_name,workflow_version,verifier_version,input_goal_text) "
        "VALUES (:id,:org,:project,:member,'RUNNING','planning','1','1','raw goal')",
        {
            "id": run,
            "org": h.actor.organization_id,
            "project": h.project_id,
            "member": h.actor.membership_id,
        },
    )
    started, release = asyncio.Event(), asyncio.Event()

    class Output(BaseModel):
        answer: str

    class Gateway:
        calls = 0

        async def generate_structured[T: BaseModel](
            self, request: StructuredModelRequest[T]
        ) -> StructuredModelResponse[T]:
            self.calls += 1
            started.set()
            await release.wait()
            return StructuredModelResponse(
                parsed=request.output_schema.model_validate({"answer": "stale"}),
                model_ref="mock:test",
            )

    sessions = async_sessionmaker(h.engine, expire_on_commit=False)
    provider = Gateway()
    gateway = WorkflowRecordingModelGateway(
        gateway=provider,
        transaction_factory=PostgreSQLPlanningRunTransactionFactory(sessions),
        organization_id=h.actor.organization_id,
        workflow_run_id=run,
    )
    request = StructuredModelRequest(
        invocation_key="retention.test", messages=(), output_schema=Output, timeout_seconds=5
    )
    operation = asyncio.create_task(gateway.generate_structured(request))
    await asyncio.wait_for(started.wait(), 5)
    await RetentionService(RetentionTransactions(sessions)).purge_once(
        organization_id=h.actor.organization_id, now=datetime.now(UTC) + timedelta(days=31)
    )
    release.set()
    with pytest.raises(ValueError, match="CONTEXT_EXPIRED"):
        await operation
    with pytest.raises(ValueError, match="CONTEXT_EXPIRED"):
        await gateway.generate_structured(request)
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_concurrent_cleanup_skip_locked_and_stale_fence(report_harness: ReportHarness):
    from app.modules.feedback.adapters.payload_repository import RetentionTransactions
    from app.modules.feedback.application.retention_service import RetentionService

    h = report_harness
    triggers, trigger = await inputs(h)
    await triggers.ensure(actor=h.actor, trigger=trigger)
    transactions = RetentionTransactions(async_sessionmaker(h.engine, expire_on_commit=False))
    now = datetime.now(UTC) + timedelta(days=31)
    async with transactions(h.actor.organization_id) as first:
        claimed = await first.claim_expired(h.actor.organization_id, now, 100)
        assert len(claimed) == 1
        assert (
            await RetentionService(transactions).purge_once(
                organization_id=h.actor.organization_id, now=now
            )
        ).purged == 0
        with pytest.raises(ValueError, match="RETENTION_FENCE_LOST"):
            await first.purge(claimed[0], claimed[0].fence + 1)
        await first.purge(claimed[0], claimed[0].fence)
        await first.evidence(1, now)
    assert (
        await RetentionService(transactions).purge_once(
            organization_id=h.actor.organization_id, now=now
        )
    ).purged == 0
