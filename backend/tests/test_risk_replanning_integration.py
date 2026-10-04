"""Exact risk/current-plan binding, immutable weekly diffs and approval-only apply."""

import os
from datetime import date
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.planning_runs.adapters.ai_runtime import PlanningAIRuntime
from app.modules.planning_runs.adapters.transaction import PostgreSQLPlanningRunTransactionFactory
from app.modules.planning_runs.application.approval_ports import ApprovalDecision
from app.modules.planning_runs.application.approval_service import ApprovalService
from app.modules.planning_runs.application.proposal_service import ProposalService
from tests.test_daily_update_api_integration import seed_task
from tests.test_evidence_api_integration import Harness, harness
from tests.test_phase4_context_permissions import reader
from tests.test_risk_api_integration import setup

__all__ = ["harness"]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


async def prepared(h: Harness):
    task = UUID(await seed_task(h))
    project = (await h.sql("SELECT project_id FROM tasks WHERE id=:id", {"id": task})).scalar_one()
    weeks = [uuid4(), uuid4(), uuid4()]
    for n, week in enumerate(weeks):
        await h.sql(
            "INSERT INTO project_weeks (id,organization_id,project_id,week_number,start_date,"
            "end_date,objective,status,created_by_membership_id,updated_by_membership_id) "
            "VALUES (:id,:org,:p,:n,:s,:e,'Deliver',:status,:m,:m)",
            {
                "id": week,
                "org": h.actor.organization_id,
                "p": project,
                "n": n + 1,
                "s": date(2026, 10, 5 + n * 7),
                "e": date(2026, 10, 11 + n * 7),
                "status": "COMPLETED" if n == 0 else "PLANNED",
                "m": h.peer.membership_id,
            },
        )
    await h.sql(
        "UPDATE tasks SET project_week_id=:week,estimated_effort_hours=8,"
        "due_date='2026-10-18' WHERE id=:id",
        {"id": task, "week": weeks[1]},
    )
    _, risks, _, manager = await setup(h)
    await risks.request_refresh(manager, task, str(uuid4()))
    assert await risks.run_once(manager)
    context = await reader(h).read(manager, task)
    binding: dict[str, object] = {
        "task_id": str(task),
        "risk_assessment_id": str(context.risk_assessment_id),
        "fingerprint": context.fingerprint,
        "observation_ids": [o.id for o in context.observations],
        "affected_week_ids": [str(w) for w in context.affected_week_ids],
    }
    sessions = async_sessionmaker(
        bind=h.connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    transactions = PostgreSQLPlanningRunTransactionFactory(sessions)
    return task, project, weeks, manager, binding, transactions


async def business(h: Harness):
    return (
        await h.sql(
            "SELECT jsonb_build_object('tasks',"
            "(SELECT jsonb_agg(to_jsonb(t) ORDER BY id) FROM tasks t WHERE organization_id=:org),"
            "'weeks',(SELECT jsonb_agg(to_jsonb(w) ORDER BY id) "
            "FROM project_weeks w WHERE organization_id=:org),"
            "'baselines',(SELECT jsonb_agg(to_jsonb(b) ORDER BY id) "
            "FROM weekly_plan_baselines b WHERE organization_id=:org))",
            {"org": h.actor.organization_id},
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_rejected_risk_replan_has_no_business_side_effect(harness: Harness):
    task, _, weeks, manager, binding, transactions = await prepared(harness)
    before = await business(harness)
    service = ProposalService(transaction_factory=transactions, runtime=PlanningAIRuntime())
    result = await service.request_risk_revision(
        actor=manager,
        risk_context=binding,
        instruction="Move the task to the next open week",
        locale="en",
        request_id="risk-test",
        idempotency_key="risk-test",
    )
    assert await business(harness) == before
    from app.modules.planning_runs.adapters.risk_replanning import RiskReplanningJobHandler
    from work_management_ai.model_gateway.mock import MockModelGateway

    class Actors:
        async def resolve(self, **kwargs: Any):
            return manager

    output = {
        "task_changes": [
            {
                "task_id": str(task),
                "project_week_id": str(weeks[2]),
                "due_date": "2026-10-25",
                "estimated_effort_hours": 8,
            }
        ],
        "new_tasks": [],
        "change_summary": "Move work to next week",
    }
    handler = RiskReplanningJobHandler(
        transaction_factory=transactions,
        actor_resolver=Actors(),
        gateway_factory=lambda run: MockModelGateway(fixtures={"planning.en.risk_replan": output}),
    )
    async with transactions(manager) as tx:
        job = await tx.repository.get_risk_revision_job(actor=manager, run_id=result.run.id)
    assert job is not None
    await handler(job=job, worker_id="test")
    async with transactions(manager) as tx:
        proposal = await tx.repository.get_proposal_by_run_id(actor=manager, run_id=result.run.id)
    assert proposal is not None and proposal.approval_id is not None
    assert await business(harness) == before
    await ApprovalService(transaction_factory=transactions, runtime=PlanningAIRuntime()).decide(
        actor=manager,
        approval_id=proposal.approval_id,
        decision=ApprovalDecision.REJECT,
        expected_proposal_version=1,
        reason="Keep current plan",
        request_id="reject",
        idempotency_key="reject",
    )
    assert await business(harness) == before


async def generated(
    h: Harness,
    *,
    new_task: bool = False,
    target_completed: bool = False,
    provider_error: Exception | None = None,
):
    from app.modules.planning_runs.adapters.risk_replanning import RiskReplanningJobHandler
    from work_management_ai.model_gateway.mock import MockModelGateway

    task, project, weeks, manager, binding, transactions = await prepared(h)
    service = ProposalService(transaction_factory=transactions, runtime=PlanningAIRuntime())
    result = await service.request_risk_revision(
        actor=manager,
        risk_context=binding,
        instruction="Reschedule delivery",
        locale="en",
        request_id="risk",
        idempotency_key="risk",
    )
    target = weeks[0] if target_completed else weeks[2]
    output = {
        "task_changes": [
            {
                "task_id": str(task),
                "project_week_id": str(target),
                "due_date": "2026-10-11" if target_completed else "2026-10-25",
                "estimated_effort_hours": 8,
            }
        ],
        "new_tasks": [
            {
                "project_week_id": str(weeks[1]),
                "title": "Check delivery",
                "description": None,
                "due_date": "2026-10-18",
                "estimated_effort_hours": 2,
                "acceptance_criteria": ["Delivery reviewed"],
            }
        ]
        if new_task
        else [],
        "change_summary": "Reschedule delivery",
    }

    class Actors:
        async def resolve(self, **kwargs: object):
            return manager

    handler = RiskReplanningJobHandler(
        transaction_factory=transactions,
        actor_resolver=Actors(),
        gateway_factory=lambda run: MockModelGateway(
            fixtures={"planning.en.risk_replan": provider_error or output}
        ),
    )
    async with transactions(manager) as tx:
        job = await tx.repository.get_risk_revision_job(actor=manager, run_id=result.run.id)
    assert job is not None
    if target_completed or provider_error:
        with pytest.raises((ValueError, RuntimeError)):
            await handler(job=job, worker_id="test")
    else:
        await handler(job=job, worker_id="test")
    async with transactions(manager) as tx:
        proposal = await tx.repository.get_proposal_by_run_id(actor=manager, run_id=result.run.id)
    return (
        task,
        project,
        weeks,
        manager,
        binding,
        transactions,
        service,
        result,
        handler,
        job,
        proposal,
    )


@pytest.mark.asyncio
async def test_risk_replan_approval_updates_existing_project_once_and_preserves_original_baseline(
    harness: Harness,
):
    task, project, weeks, manager, _, transactions, _, _, handler, job, proposal = await generated(
        harness, new_task=True
    )
    assert proposal is not None and proposal.approval_id is not None
    before = (
        await harness.sql(
            "SELECT row_to_json(w) FROM project_weeks w WHERE id=:id", {"id": weeks[0]}
        )
    ).scalar_one()
    service = ApprovalService(transaction_factory=transactions, runtime=PlanningAIRuntime())
    values: dict[str, Any] = dict(
        actor=manager,
        approval_id=proposal.approval_id,
        decision=ApprovalDecision.APPROVE,
        expected_proposal_version=1,
        reason=None,
        request_id="approve",
        idempotency_key="approve",
    )
    result = await service.decide(**values)
    replay = await service.decide(**values)
    assert replay.replayed and replay.created == result.created
    assert result.created.project_id == project and len(result.created.task_ids) == 1
    row = (
        await harness.sql(
            "SELECT project_week_id,version,assignee_membership_id FROM tasks WHERE id=:id",
            {"id": task},
        )
    ).one()
    assert row[0] == weeks[2] and row[1] == 2 and row[2] == harness.actor.membership_id
    assert (
        await harness.sql(
            "SELECT count(*) FROM projects WHERE organization_id=:org",
            {"org": manager.organization_id},
        )
    ).scalar_one() == 1
    assert (
        await harness.sql(
            "SELECT assignee_membership_id FROM tasks WHERE id=:id",
            {"id": result.created.task_ids[0]},
        )
    ).scalar_one() is None
    assert (
        await harness.sql(
            "SELECT row_to_json(w) FROM project_weeks w WHERE id=:id", {"id": weeks[0]}
        )
    ).scalar_one() == before
    snapshots = (
        await harness.sql(
            "SELECT sequence,payload FROM weekly_plan_baselines "
            "WHERE project_week_id=:id ORDER BY sequence",
            {"id": weeks[1]},
        )
    ).all()
    assert len(snapshots) == 2
    assert snapshots[0][1]["task_entries"][0]["task_id"] == str(task)
    assert snapshots[1][1]["task_entries"][0]["task_id"] == str(result.created.task_ids[0])
    stable = await business(harness)
    await handler(job=job, worker_id="replay")
    assert await business(harness) == stable


@pytest.mark.asyncio
async def test_completed_week_changes_are_denied_before_proposal_creation(harness: Harness):
    *_, result, _, _, proposal = await generated(harness, target_completed=True)
    assert proposal is None
    assert (
        await harness.sql("SELECT status FROM workflow_runs WHERE id=:id", {"id": result.run.id})
    ).scalar_one() == "FAILED"
    assert (
        await harness.sql(
            "SELECT count(*) FROM tasks WHERE version<>1 AND organization_id=:org",
            {"org": result.run.organization_id},
        )
    ).scalar_one() == 0


@pytest.mark.asyncio
async def test_provider_timeout_leaves_manual_plan_and_a_safe_fallback(harness: Harness):
    from work_management_ai.model_gateway.errors import ModelTimeoutError

    *_, result, _, _, proposal = await generated(
        harness, provider_error=ModelTimeoutError("timeout")
    )
    assert proposal is None
    assert (
        await harness.sql("SELECT status FROM workflow_runs WHERE id=:id", {"id": result.run.id})
    ).scalar_one() == "FAILED"
    event = (
        await harness.sql(
            "SELECT public_payload FROM workflow_events "
            "WHERE workflow_run_id=:id AND event_type='workflow.failed'",
            {"id": result.run.id},
        )
    ).scalar_one()
    assert event["manual_fallback"] == "PROJECT_TASK_EDITOR"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["title", "move", "assignment", "completed_week"])
async def test_stale_risk_or_plan_cannot_apply_or_edit_and_rejection_is_audited(
    harness: Harness,
    mutation: str,
):
    from app.modules.planning_runs.domain.models import ProposalStaleError

    task, _, weeks, manager, _, transactions, _, _, _, _, proposal = await generated(harness)
    assert proposal is not None and proposal.approval_id is not None
    if mutation == "completed_week":
        await harness.sql(
            "UPDATE project_weeks SET status='COMPLETED',version=version+1 WHERE id=:id",
            {"id": weeks[1]},
        )
    else:
        change = {
            "title": "title='Updated manually'",
            "move": "project_week_id=:week",
            "assignment": "assignee_membership_id=NULL",
        }[mutation]
        await harness.sql(
            "UPDATE tasks SET " + change + ",version=version+1 WHERE id=:id",
            {"id": task, "week": weeks[2]},
        )
    before = await business(harness)
    with pytest.raises(ProposalStaleError):
        await ApprovalService(transaction_factory=transactions, runtime=PlanningAIRuntime()).decide(
            actor=manager,
            approval_id=proposal.approval_id,
            decision=ApprovalDecision.APPROVE,
            expected_proposal_version=1,
            reason=None,
            request_id="stale",
            idempotency_key="stale",
        )
    assert await business(harness) == before
    assert (
        await harness.sql("SELECT count(*) FROM audit_events WHERE outcome='REJECTED'")
    ).scalar_one() > 0


@pytest.mark.asyncio
async def test_request_replay_role_tenant_and_binding_checks(harness: Harness):
    from app.modules.planning_runs.domain.models import (
        IdempotencyKeyReusedError,
        PlanningRunForbiddenError,
        PlanningRunNotFoundError,
        ProposalStaleError,
    )

    _, _, _, manager, binding, transactions = await prepared(harness)
    service = ProposalService(transaction_factory=transactions, runtime=PlanningAIRuntime())
    values: dict[str, Any] = dict(
        actor=manager,
        risk_context=binding,
        instruction="Reschedule",
        locale="vi",
        request_id="request",
        idempotency_key="request",
    )
    first = await service.request_risk_revision(**values)
    replay = await service.request_risk_revision(**values)
    assert replay.replayed and replay.run.id == first.run.id
    with pytest.raises(IdempotencyKeyReusedError):
        await service.request_risk_revision(
            **cast(dict[str, Any], {**values, "instruction": "Different change"})
        )
    with pytest.raises(PlanningRunForbiddenError):
        await service.request_risk_revision(
            **cast(dict[str, Any], {**values, "actor": harness.actor})
        )
    with pytest.raises((PlanningRunForbiddenError, PlanningRunNotFoundError)):
        await service.request_risk_revision(
            **cast(dict[str, Any], {**values, "actor": harness.foreign})
        )
    with pytest.raises(ProposalStaleError):
        await service.request_risk_revision(
            **cast(
                dict[str, Any],
                {
                    **values,
                    "risk_context": {**binding, "fingerprint": "b" * 64},
                    "idempotency_key": "wrong",
                },
            )
        )
    await harness.sql(
        "UPDATE memberships SET role='EMPLOYEE' WHERE id=:id", {"id": manager.membership_id}
    )
    with pytest.raises(PlanningRunForbiddenError):
        await service.request_risk_revision(**values)


@pytest.mark.asyncio
async def test_editing_preserves_source_binding_and_requires_a_new_approval(harness: Harness):
    from copy import deepcopy

    _, _, _, manager, _, transactions, service, _, _, _, proposal = await generated(harness)
    assert proposal is not None and proposal.approval_id is not None
    async with transactions(manager) as tx:
        version = await tx.repository.get_proposal_version(
            actor=manager, proposal_id=proposal.id, version_number=1
        )
    assert version is not None
    content = deepcopy(version.content)
    content["tasks"][0]["estimated_effort_hours"] = 10
    edited = await service.edit_proposal(
        actor=manager,
        proposal_id=proposal.id,
        expected_version=1,
        content=content,
        request_id="edit",
        idempotency_key="edit",
    )
    assert edited.version.version_number == 2
    from app.modules.identity.domain.auth import AuthenticatedActor
    from app.modules.planning_runs.adapters.ai_runtime import ProposalRevalidationJobHandler
    from app.modules.planning_runs.domain.models import WorkflowJob, WorkflowJobStatus

    class Actors:
        async def resolve(self, **kwargs: object) -> AuthenticatedActor:
            return manager

    await ProposalRevalidationJobHandler(transactions, Actors())(
        job=WorkflowJob(
            id=uuid4(),
            organization_id=manager.organization_id,
            workflow_run_id=proposal.workflow_run_id,
            job_type="proposal.revalidate",
            status=WorkflowJobStatus.RUNNING,
            payload={"proposal_id": str(proposal.id), "proposal_version": 2},
        ),
        worker_id="test",
    )
    async with transactions(manager) as tx:
        current = await tx.repository.get_proposal(actor=manager, proposal_id=proposal.id)
    assert current is not None and current.approval_id != proposal.approval_id
    tampered = deepcopy(content)
    tampered["risk_replan"]["binding"]["fingerprint"] = "b" * 64
    with pytest.raises(ValueError, match="RISK_PLAN_BINDING_IMMUTABLE"):
        await service.edit_proposal(
            actor=manager,
            proposal_id=proposal.id,
            expected_version=2,
            content=tampered,
            request_id="tamper",
            idempotency_key="tamper",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_failure", [False, True])
async def test_ai_revision_of_risk_proposal_retains_exact_sources(
    harness: Harness,
    provider_failure: bool,
):
    _, _, _, manager, _, transactions, service, result, handler, _, proposal = await generated(
        harness
    )
    assert proposal is not None
    revision = await service.request_ai_revision(
        actor=manager,
        proposal_id=proposal.id,
        expected_version=1,
        instruction="Make a smaller schedule adjustment",
        request_id="revision",
        idempotency_key="revision",
    )
    job = await harness.sql(
        "SELECT job_type,payload FROM workflow_jobs WHERE id=:id", {"id": revision.revision_job_id}
    )
    row = job.one()
    assert row[0] == "proposal.risk_replan_revision"
    from app.modules.planning_runs.domain.models import WorkflowJob, WorkflowJobStatus

    revision_job = WorkflowJob(
        id=revision.revision_job_id,
        organization_id=manager.organization_id,
        workflow_run_id=result.run.id,
        job_type=row[0],
        status=WorkflowJobStatus.RUNNING,
        payload=row[1],
    )
    if provider_failure:
        from work_management_ai.model_gateway.mock import MockModelGateway

        def failing_gateway(run: object) -> MockModelGateway:
            del run
            return MockModelGateway(
                fixtures={"planning.en.risk_replan": RuntimeError("synthetic unavailable")}
            )

        handler.gateways = failing_gateway
        with pytest.raises(RuntimeError):
            await handler(job=revision_job, worker_id="revision")
        async with transactions(manager) as tx:
            unchanged = await tx.repository.get_proposal(actor=manager, proposal_id=proposal.id)
        assert unchanged == proposal
        failure = (
            await harness.sql(
                "SELECT public_payload FROM workflow_events WHERE workflow_run_id=:id "
                "AND event_type='proposal.revision_failed'",
                {"id": result.run.id},
            )
        ).scalar_one()
        assert failure["manual_fallback"] == "PROJECT_TASK_EDITOR"
        return
    await handler(job=revision_job, worker_id="revision")

    async with transactions(manager) as tx:
        current = await tx.repository.get_proposal(actor=manager, proposal_id=proposal.id)
        old = await tx.repository.get_proposal_version(
            actor=manager, proposal_id=proposal.id, version_number=1
        )
        revised = await tx.repository.get_proposal_version(
            actor=manager, proposal_id=proposal.id, version_number=2
        )
    assert current is not None and current.current_version_number == 2
    assert old is not None and revised is not None
    assert old.source_reference_snapshot == revised.source_reference_snapshot
    assert old.content["risk_replan"] == revised.content["risk_replan"]


@pytest.mark.asyncio
async def test_expired_final_risk_job_stops_with_manual_fallback(harness: Harness):
    from datetime import UTC, datetime, timedelta

    _, _, _, manager, binding, transactions = await prepared(harness)
    service = ProposalService(transaction_factory=transactions, runtime=PlanningAIRuntime())
    result = await service.request_risk_revision(
        actor=manager,
        risk_context=binding,
        instruction="Reschedule delivery",
        locale="en",
        request_id="expired",
        idempotency_key="expired",
    )
    now = datetime.now(UTC)
    async with transactions(manager.organization_id) as tx:
        claimed = await tx.repository.claim_job(
            organization_id=manager.organization_id,
            worker_id="dead-worker",
            now=now,
            lease_until=now + timedelta(seconds=60),
        )
        await tx.commit()
    assert claimed is not None and claimed.lease_until is not None
    assert (claimed.lease_until - now).total_seconds() >= 150
    await harness.sql(
        "UPDATE workflow_jobs SET status='RUNNING',attempt_count=max_attempts,"
        "lease_until=:expired,locked_by_worker_id='dead-worker' WHERE workflow_run_id=:run",
        {"expired": datetime.now(UTC) - timedelta(minutes=10), "run": result.run.id},
    )
    async with transactions(manager.organization_id) as tx:
        await tx.repository.claim_job(
            organization_id=manager.organization_id,
            worker_id="replacement",
            now=datetime.now(UTC),
            lease_until=datetime.now(UTC) + timedelta(minutes=3),
        )
        await tx.commit()
    state = (
        await harness.sql(
            "SELECT status FROM workflow_runs WHERE id=:run",
            {"run": result.run.id},
        )
    ).scalar_one()
    assert state == "FAILED"
    event = (
        await harness.sql(
            "SELECT public_payload FROM workflow_events WHERE workflow_run_id=:run "
            "AND event_type='workflow.failed'",
            {"run": result.run.id},
        )
    ).scalar_one()
    assert event["manual_fallback"] == "PROJECT_TASK_EDITOR"


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", ["ref", "effort", "criteria"])
async def test_malformed_manual_risk_edit_is_rejected_before_approval(
    harness: Harness,
    malformed: str,
):
    from copy import deepcopy

    from app.modules.planning_runs.domain.risk_replanning import validate_content

    _, _, _, manager, _, transactions, _, _, _, _, proposal = await generated(
        harness,
        new_task=True,
    )
    assert proposal is not None
    async with transactions(manager) as tx:
        version = await tx.repository.get_proposal_version(
            actor=manager,
            proposal_id=proposal.id,
            version_number=1,
        )
    assert version is not None
    content = deepcopy(version.content)
    if malformed == "ref":
        content["tasks"][0]["ref"] = []
    elif malformed == "effort":
        content["tasks"][0]["estimated_effort_hours"] = "8"
    else:
        content["tasks"][-1]["acceptance_criteria"] = ["   "]
    with pytest.raises(ValueError):
        validate_content(content)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["edit", "decision"])
@pytest.mark.parametrize("revocation", ["role", "active"])
async def test_risk_mutation_replay_rechecks_current_manager(
    harness: Harness,
    operation: str,
    revocation: str,
):
    from copy import deepcopy

    from app.modules.planning_runs.domain.models import PlanningRunForbiddenError

    _, _, _, manager, _, transactions, service, _, _, _, proposal = await generated(harness)
    assert proposal is not None
    approvals = ApprovalService(transaction_factory=transactions, runtime=PlanningAIRuntime())
    if operation == "edit":
        async with transactions(manager) as tx:
            version = await tx.repository.get_proposal_version(
                actor=manager,
                proposal_id=proposal.id,
                version_number=1,
            )
        assert version is not None
        content = deepcopy(version.content)
        content["tasks"][0]["estimated_effort_hours"] = 9
        values = dict(
            actor=manager,
            proposal_id=proposal.id,
            expected_version=1,
            content=content,
            request_id="replay-edit",
            idempotency_key="replay-edit",
        )
        await service.edit_proposal(**cast(dict[str, Any], values))
    else:
        approvals = ApprovalService(transaction_factory=transactions, runtime=PlanningAIRuntime())
        values = dict(
            actor=manager,
            approval_id=proposal.approval_id,
            decision=ApprovalDecision.REJECT,
            expected_proposal_version=1,
            reason="Retain plan",
            request_id="replay-decision",
            idempotency_key="replay-decision",
        )
        await approvals.decide(**cast(dict[str, Any], values))
    await harness.sql(
        "UPDATE memberships SET "
        + ("role='EMPLOYEE'" if revocation == "role" else "is_active=false")
        + " WHERE id=:id",
        {"id": manager.membership_id},
    )
    with pytest.raises(PlanningRunForbiddenError):
        if operation == "edit":
            await service.edit_proposal(**cast(dict[str, Any], values))
        else:
            await approvals.decide(**cast(dict[str, Any], values))
