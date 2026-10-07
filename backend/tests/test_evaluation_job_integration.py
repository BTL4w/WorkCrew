import asyncio
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from app.modules.feedback.adapters.evaluation_runner import ReportingEvaluationProvider
from app.modules.feedback.domain.evaluation import EvaluationDatasetVersion, ReportEvaluationResult
from app.modules.feedback.domain.evaluation_runs import EvaluationRequest
from app.modules.reporting.domain.reports import ReportError
from tests.test_evaluation_api_integration import evaluation_service
from tests.test_report_api_integration import ReportHarness, pytestmark, report_harness
from tests.test_report_evaluation_runner_integration import frozen_dataset

__all__ = ["pytestmark", "report_harness"]


@pytest.mark.asyncio
async def test_eval_status_redacts_raw_context(report_harness: ReportHarness):
    h = report_harness
    dataset = await frozen_dataset(h)
    service = evaluation_service(h)
    run = await service.start(
        actor=h.actor,
        request=EvaluationRequest(dataset_version_id=dataset.id),
        idempotency_key=str(uuid4()),
    )
    assert await service.run_once(worker_id="worker", organization_id=h.actor.organization_id)
    result = await service.get(actor=h.actor, run_id=run.id)
    assert result.status == "FAILED" and result.failure_kind == "GATE"
    assert result.result is not None and result.result.passed == 2
    assert result.result.hosted_quality == "NOT_RUN"
    assert len(result.result.cases) == 2
    for value in ('"narrative":', '"prompt":{', '"context":', '"messages":', "api_key"):
        assert value not in result.model_dump_json()
    assert not await service.run_once(worker_id="worker", organization_id=h.actor.organization_id)
    rows = await h.sql(
        "SELECT count(*) FROM evaluation_results WHERE organization_id=:org AND run_id=:id",
        {"org": h.actor.organization_id, "id": run.id},
    )
    assert rows.scalar_one() == 2


@pytest.mark.asyncio
async def test_eval_worker_rechecks_admin_and_fence(report_harness: ReportHarness):
    h = report_harness
    dataset = await frozen_dataset(h)
    calls = 0
    entered, release = asyncio.Event(), asyncio.Event()

    class Provider:
        async def evaluate(self, dataset: EvaluationDatasetVersion) -> ReportEvaluationResult:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return await ReportingEvaluationProvider().evaluate(dataset)

    service = evaluation_service(h, provider=Provider())
    request = EvaluationRequest(dataset_version_id=dataset.id)
    run = await service.start(actor=h.actor, request=request, idempotency_key=str(uuid4()))
    work = asyncio.create_task(
        service.run_once(worker_id="one", organization_id=h.actor.organization_id)
    )
    await asyncio.wait_for(entered.wait(), 10)
    assert not await service.run_once(worker_id="two", organization_id=h.actor.organization_id)
    await h.sql(
        "UPDATE memberships SET role='EMPLOYEE' WHERE id=:id", {"id": h.actor.membership_id}
    )
    release.set()
    assert await work
    state = await h.sql("SELECT status FROM evaluation_runs WHERE id=:id", {"id": run.id})
    assert state.scalar_one() == "CANCELLED"
    rows = await h.sql("SELECT count(*) FROM evaluation_results WHERE run_id=:id", {"id": run.id})
    assert rows.scalar_one() == 0 and calls == 1
    await h.sql("UPDATE memberships SET role='ADMIN' WHERE id=:id", {"id": h.actor.membership_id})
    run = await service.start(actor=h.actor, request=request, idempotency_key=str(uuid4()))
    old = await service.claim(worker_id="old", organization_id=h.actor.organization_id)
    assert old is not None
    await h.sql(
        "UPDATE evaluation_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id",
        {"id": run.id},
    )
    current = await service.claim(worker_id="new", organization_id=h.actor.organization_id)
    assert current is not None and current.fence > old.fence
    with pytest.raises(ReportError, match="EVALUATION_FENCE_LOST"):
        await service.authorize(old)
    await h.sql(
        "UPDATE memberships SET role='EMPLOYEE' WHERE id=:id", {"id": h.actor.membership_id}
    )
    await service.execute(current)
    assert calls == 1


@pytest.mark.asyncio
async def test_eval_rejects_unmeasured_or_raw_provider_payload(report_harness: ReportHarness):
    h = report_harness
    dataset = await frozen_dataset(h)

    class RawProvider:
        async def evaluate(self, dataset: EvaluationDatasetVersion) -> ReportEvaluationResult:
            result = await ReportingEvaluationProvider().evaluate(dataset)
            cases = tuple({**item, "raw_context": "PRIVATE_RAW_SENTINEL"} for item in result.cases)
            return result.model_copy(update={"cases": cases})

    service = evaluation_service(h, provider=RawProvider())
    run = await service.start(
        actor=h.actor,
        request=EvaluationRequest(dataset_version_id=dataset.id),
        idempotency_key=str(uuid4()),
    )
    await service.run_once(worker_id="raw", organization_id=h.actor.organization_id)
    value = await service.get(actor=h.actor, run_id=run.id)
    assert value.status == "FAILED" and value.failure_kind == "WORKER" and value.result is None
    assert "PRIVATE_RAW_SENTINEL" not in value.model_dump_json()


@pytest.mark.asyncio
async def test_eval_result_lineage_rls_and_immutability(report_harness: ReportHarness):
    h = report_harness
    dataset = await frozen_dataset(h)
    service = evaluation_service(h)
    run = await service.start(
        actor=h.actor,
        request=EvaluationRequest(dataset_version_id=dataset.id),
        idempotency_key=str(uuid4()),
    )
    await service.run_once(worker_id="rls", organization_id=h.actor.organization_id)
    from sqlalchemy import text

    for actor in (h.employee, h.foreign):
        async with h.engine.begin() as conn:
            await conn.execute(text("SET LOCAL ROLE app_runtime"))
            await conn.execute(
                text(
                    "SELECT "
                    "set_config('app.organization_id',:org,true),set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            assert (
                await conn.execute(text("SELECT count(*) FROM evaluation_runs"))
            ).scalar_one() == 0
            assert (
                await conn.execute(text("SELECT count(*) FROM evaluation_results"))
            ).scalar_one() == 0
    from sqlalchemy.exc import DBAPIError

    for statement in (
        "UPDATE evaluation_runs SET dataset_hash=repeat('b',64) WHERE id=:id",
        "UPDATE evaluation_runs SET status='QUEUED' WHERE id=:id",
        "DELETE FROM evaluation_results WHERE run_id=:id",
        "UPDATE evaluation_results SET case_hash=repeat('b',64) WHERE run_id=:id",
    ):
        with pytest.raises(DBAPIError):
            await h.sql(statement, {"id": run.id})


@pytest.mark.asyncio
async def test_eval_worker_consumes_run_and_outbox_with_explicit_allowlist(
    report_harness: ReportHarness,
):
    from dataclasses import replace

    from app.modules.feedback.adapters.outbox_consumer import FeedbackOutboxPublisher
    from app.modules.planning_runs.domain.models import OutboxEvent

    h = report_harness
    dataset = await frozen_dataset(h)
    service = evaluation_service(h)
    run = await service.start(
        actor=h.actor,
        request=EvaluationRequest(dataset_version_id=dataset.id),
        idempotency_key=str(uuid4()),
    )

    class Delegate:
        async def publish(self, event: OutboxEvent) -> None:
            raise NotImplementedError(event.event_type)

    publisher = FeedbackOutboxPublisher(Delegate())
    event = OutboxEvent(
        id=uuid4(),
        event_id=uuid4(),
        organization_id=h.actor.organization_id,
        event_type="evaluation.run.requested.v1",
        aggregate_type="evaluation",
        aggregate_id=run.id,
        payload={
            "schema_version": "1.0",
            "id": str(run.id),
            "policy_version": "report-eval-provider.v1",
        },
    )
    await publisher.publish(event)
    with pytest.raises(NotImplementedError):
        await publisher.publish(replace(event, event_type="evaluation.unknown.v1"))
    with pytest.raises(ValueError):
        await publisher.publish(replace(event, payload={**event.payload, "raw_context": "private"}))
    from app.worker import process_tenant_once

    class NoJobs:
        async def run_once(self, worker_id: str, organization_id: UUID) -> bool:
            return False

        async def dispatch_once(self, worker_id: str, organization_id: UUID) -> bool:
            return False

    empty = NoJobs()
    assert await process_tenant_once(
        worker_id="w",
        organization_id=h.actor.organization_id,
        outbox_service=empty,
        assistant_job_service=empty,
        planning_job_service=empty,
        evaluation_job_service=service,
    )


@pytest.mark.asyncio
async def test_eval_hosted_expired_claim_never_resets_budget(report_harness: ReportHarness):
    from app.core.config import Settings
    from app.modules.feedback.adapters.evaluation_policy import EvaluationPolicy
    from app.modules.feedback.application.evaluation_service import EvaluationService

    h = report_harness
    dataset = await frozen_dataset(h)
    calls = 0

    class Provider:
        async def evaluate(self, dataset: EvaluationDatasetVersion) -> ReportEvaluationResult:
            nonlocal calls
            calls += 1
            raise AssertionError("Hosted transport must not be retried after losing its lease")

    settings = Settings(
        environment="test",
        ai_provider="openai",
        ai_model="configured-model",
        openai_api_key=SecretStr("synthetic-test-key"),
        report_evaluation_hosted_enabled=True,
        report_evaluation_budget_tokens=54321,
    )
    base = evaluation_service(h)
    service = EvaluationService(
        base.transactions, policy=EvaluationPolicy(settings), provider=Provider()
    )
    run = await service.start(
        actor=h.actor,
        request=EvaluationRequest(dataset_version_id=dataset.id, provider="hosted"),
        idempotency_key=str(uuid4()),
    )
    assert run.budget_tokens == 54321 and run.provider_policy_version == "report-eval-provider.v1"
    job = await service.claim(worker_id="old", organization_id=h.actor.organization_id)
    assert job is not None
    await h.sql(
        "UPDATE evaluation_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id",
        {"id": run.id},
    )
    assert await service.run_once(worker_id="new", organization_id=h.actor.organization_id)
    result = await service.get(actor=h.actor, run_id=run.id)
    assert result.status == "FAILED" and result.failure_kind == "WORKER"
    assert result.safe_error_code == "EVALUATION_LEASE_EXPIRED" and calls == 0


@pytest.mark.asyncio
async def test_eval_concurrent_start_has_one_job_and_one_request_event(
    report_harness: ReportHarness,
):
    h = report_harness
    dataset = await frozen_dataset(h)
    service = evaluation_service(h)
    key = str(uuid4())

    async def start():
        return await service.start(
            actor=h.actor,
            request=EvaluationRequest(dataset_version_id=dataset.id),
            idempotency_key=key,
        )

    first, second = await asyncio.gather(start(), start())
    assert first.id == second.id and first.replayed != second.replayed
    result = await h.sql(
        "SELECT count(*) FROM evaluation_runs WHERE organization_id=:org",
        {"org": h.actor.organization_id},
    )
    assert result.scalar_one() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["provenance", "quality", "limitations", "versions"])
async def test_eval_nested_metadata_cannot_persist_raw_context(
    report_harness: ReportHarness, mutation: str
):
    h = report_harness
    dataset = await frozen_dataset(h)

    class RawMetadata:
        async def evaluate(self, dataset: EvaluationDatasetVersion) -> ReportEvaluationResult:
            result = await ReportingEvaluationProvider().evaluate(dataset)
            if mutation == "quality":
                return result.model_copy(update={"hosted_quality": "PRIVATE_RAW_SENTINEL"})
            if mutation == "limitations":
                return result.model_copy(update={"limitations": ("PRIVATE_RAW_SENTINEL",)})
            cases = [dict(item) for item in result.cases]
            if mutation == "provenance":
                source = cases[0]["provenance"]
                assert isinstance(source, dict)
                provenance = dict(source)
                provenance["split"] = {"raw_context": "PRIVATE_RAW_SENTINEL"}
                cases[0]["provenance"] = provenance
            else:
                cases[0]["versions"] = {"prompt": "PRIVATE_RAW_SENTINEL"}
            return result.model_copy(update={"cases": tuple(cases)})

    service = evaluation_service(h, provider=RawMetadata())
    run = await service.start(
        actor=h.actor,
        request=EvaluationRequest(dataset_version_id=dataset.id),
        idempotency_key=str(uuid4()),
    )
    await service.run_once(worker_id="raw-metadata", organization_id=h.actor.organization_id)
    value = await service.get(actor=h.actor, run_id=run.id)
    assert value.status == "FAILED" and value.failure_kind == "WORKER" and value.result is None
    raw = await h.sql("SELECT count(*) FROM evaluation_results WHERE run_id=:id", {"id": run.id})
    assert raw.scalar_one() == 0
    assert "PRIVATE_RAW_SENTINEL" not in value.model_dump_json()


@pytest.mark.asyncio
async def test_eval_gate_coverage_order_is_not_a_worker_failure(report_harness: ReportHarness):
    h = report_harness
    dataset = await frozen_dataset(h)

    class Reordered:
        async def evaluate(self, dataset: EvaluationDatasetVersion) -> ReportEvaluationResult:
            result = await ReportingEvaluationProvider().evaluate(dataset)
            gate = dict(result.gate)
            for key in ("coverage", "required_coverage", "missing_coverage"):
                values = gate[key]
                assert isinstance(values, list)
                gate[key] = list(reversed(values))
            return result.model_copy(update={"gate": gate})

    service = evaluation_service(h, provider=Reordered())
    run = await service.start(
        actor=h.actor,
        request=EvaluationRequest(dataset_version_id=dataset.id),
        idempotency_key=str(uuid4()),
    )
    await service.run_once(worker_id="reordered", organization_id=h.actor.organization_id)
    value = await service.get(actor=h.actor, run_id=run.id)
    assert value.failure_kind == "GATE" and value.result is not None and value.result.passed == 2
