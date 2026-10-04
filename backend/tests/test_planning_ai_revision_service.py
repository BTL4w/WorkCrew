"""Task 8 exact-version AI revision request tests."""

# pyright: reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportArgumentType=false, reportAttributeAccessIssue=false

from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from typing import Self
from uuid import UUID, uuid4

import pytest

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.planning_runs.adapters.ai_runtime import ProposalAIRevisionJobHandler
from app.modules.planning_runs.application.ports import ProposalAIRevisionPreflight
from app.modules.planning_runs.application.proposal_service import ProposalService
from app.modules.planning_runs.domain.models import (
    Approval,
    IdempotencyKeyReusedError,
    PlanningRunForbiddenError,
    Proposal,
    ProposalStatus,
    ProposalVersion,
    ResourceVersionMismatchError,
    WorkflowJob,
    WorkflowJobStatus,
    WorkflowRun,
)
from work_management_ai.schemas.planning import PlanningModelOutput
from work_management_ai.workflows.planning.ports import PlanningRevisionDraft


def _actor(role: MembershipRole = MembershipRole.MANAGER) -> AuthenticatedActor:
    return AuthenticatedActor(
        user_id=uuid4(),
        email="actor@example.test",
        display_name="Actor",
        membership_id=uuid4(),
        organization_id=uuid4(),
        organization_name="Tenant",
        role=role,
    )


class _Runtime:
    workflow_version = "1.0.0"
    verifier_version = "1.0.0"

    def validate_proposal_content(self, content):
        return content

    def validate_proposal_deterministically(self, content, *, active_membership_ids):
        return {"can_approve": True, "errors": [], "warnings": []}


class _Repository:
    def __init__(self, actor: AuthenticatedActor) -> None:
        self.proposal = Proposal.create(
            organization_id=actor.organization_id,
            workflow_run_id=uuid4(),
        ).mark_ready_for_decision(uuid4())
        self.approval = Approval.create(
            id=self.proposal.approval_id,
            organization_id=actor.organization_id,
            proposal_id=self.proposal.id,
            proposal_version_number=1,
        )
        self.version = ProposalVersion(
            id=uuid4(),
            organization_id=actor.organization_id,
            proposal_id=self.proposal.id,
            version_number=1,
            created_by_membership_id=actor.membership_id,
            content={"project": {}, "goal": {}, "milestones": [], "tasks": [], "dependencies": []},
            assumptions=[],
            creator_type="AI_SYSTEM",
        )
        self.idempotency: dict[tuple[UUID, str], tuple[str, object]] = {}
        self.jobs: list[object] = []
        self.audit: list[tuple[str, str]] = []

    async def audit_rejection(self, *, action: str, reason_code: str, **_):
        self.audit.append((action, reason_code))

    async def get_proposal_version(self, *, actor, proposal_id, version_number):
        return (
            self.version
            if self.proposal.id == proposal_id and self.version.version_number == version_number
            else None
        )

    async def request_ai_revision_mutation(self, **values):
        from app.modules.planning_runs.application.ports import ProposalRevisionRequestResult

        key = (values["actor"].membership_id, values["idempotency_key"])
        existing = self.idempotency.get(key)
        if existing:
            old_fingerprint, result = existing
            if old_fingerprint != values["request_fingerprint"]:
                raise IdempotencyKeyReusedError
            return ProposalRevisionRequestResult(
                proposal_id=result.proposal_id,
                base_version=result.base_version,
                workflow_run_id=result.workflow_run_id,
                revision_job_id=result.revision_job_id,
                replayed=True,
            )
        if values["proposal_id"] != self.proposal.id:
            from app.modules.planning_runs.domain.models import PlanningRunNotFoundError

            raise PlanningRunNotFoundError
        if values["expected_version"] != self.proposal.current_version_number:
            raise ResourceVersionMismatchError(self.proposal.current_version_number)
        self.jobs.append(values["job"])
        result = ProposalRevisionRequestResult(
            proposal_id=self.proposal.id,
            base_version=1,
            workflow_run_id=self.proposal.workflow_run_id,
            revision_job_id=values["job"].id,
            replayed=False,
        )
        self.idempotency[key] = (values["request_fingerprint"], result)
        return result


class _Transaction(AbstractAsyncContextManager["_Transaction"]):
    def __init__(self, repository: _Repository) -> None:
        self.repository = repository

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_):
        return None


def _service(repository: _Repository) -> ProposalService:
    return ProposalService(
        transaction_factory=lambda _: _Transaction(repository),
        runtime=_Runtime(),
    )


@pytest.mark.asyncio
async def test_ai_revision_requires_admin_or_manager_and_exact_version() -> None:
    manager = _actor()
    repository = _Repository(manager)
    employee = _actor(MembershipRole.EMPLOYEE)
    employee = AuthenticatedActor(
        user_id=employee.user_id,
        email=employee.email,
        display_name=employee.display_name,
        membership_id=employee.membership_id,
        organization_id=manager.organization_id,
        organization_name=employee.organization_name,
        role=employee.role,
    )

    with pytest.raises(PlanningRunForbiddenError):
        await _service(repository).request_ai_revision(
            actor=employee,
            proposal_id=repository.proposal.id,
            expected_version=1,
            instruction="Move the deadline",
            request_id="employee",
            idempotency_key="employee-key",
        )

    with pytest.raises(ResourceVersionMismatchError):
        await _service(repository).request_ai_revision(
            actor=manager,
            proposal_id=repository.proposal.id,
            expected_version=2,
            instruction="Move the deadline",
            request_id="stale",
            idempotency_key="stale-key",
        )

    result = await _service(repository).request_ai_revision(
        actor=manager,
        proposal_id=repository.proposal.id,
        expected_version=1,
        instruction="  Move the deadline  ",
        request_id="valid",
        idempotency_key="valid-key",
    )
    assert result.base_version == 1
    assert len(repository.jobs) == 1
    assert repository.proposal.status is ProposalStatus.READY_FOR_DECISION
    assert repository.approval.status.value == "PENDING"


@pytest.mark.asyncio
async def test_same_idempotency_key_different_revision_payload_conflicts() -> None:
    manager = _actor()
    repository = _Repository(manager)
    service = _service(repository)
    values = {
        "actor": manager,
        "proposal_id": repository.proposal.id,
        "expected_version": 1,
        "instruction": "Move the deadline",
        "request_id": "revision",
        "idempotency_key": "same-key",
    }

    first = await service.request_ai_revision(**values)
    replay = await service.request_ai_revision(**values)
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.revision_job_id == first.revision_job_id

    with pytest.raises(IdempotencyKeyReusedError):
        await service.request_ai_revision(**{**values, "instruction": "Change scope"})


def _plan(assignee: UUID | None, *, include_new: bool = False) -> dict[str, object]:
    del assignee
    tasks: list[dict[str, object]] = [
        {
            "ref": "existing",
            "project_week_ref": "week-1",
            "title": "Existing task",
            "description": None,
            "milestone_ref": None,
            "due_date": None,
            "assignee_membership_id": None,
            "required_skill_labels": ["planning"],
            "estimated_effort_hours": 8,
            "acceptance_criteria": ["Reviewed"],
        }
    ]
    if include_new:
        tasks.append(
            {
                "ref": "new",
                "project_week_ref": "week-1",
                "title": "New task",
                "description": None,
                "milestone_ref": None,
                "due_date": None,
                "assignee_membership_id": None,
                "required_skill_labels": ["coordination"],
                "estimated_effort_hours": 4,
                "acceptance_criteria": ["Done"],
            }
        )
    return {
        "project": {
            "title": "Plan",
            "description": None,
            "start_date": None,
            "due_date": None,
        },
        "goal": {
            "title": "Goal",
            "description": None,
            "expected_outcomes": ["Outcome"],
            "target_date": None,
        },
        "milestones": [],
        "project_weeks": [
            {
                "ref": "week-1",
                "week_number": 1,
                "start_date": "2026-08-17",
                "end_date": "2026-08-23",
                "objective": "Deliver the plan",
            }
        ],
        "tasks": tasks,
        "dependencies": [],
        "assumptions": [],
    }


class _RevisionRepository:
    def __init__(self, actor: AuthenticatedActor) -> None:
        approval_id = uuid4()
        self.run = WorkflowRun.create(
            organization_id=actor.organization_id,
            project_id=None,
            requested_by_membership_id=actor.membership_id,
            workflow_name="project_planning",
            workflow_version="1.0.0",
            verifier_version="1.0.0",
            input_goal_text="Plan",
        )
        self.proposal = Proposal.create(
            organization_id=actor.organization_id,
            workflow_run_id=self.run.id,
        ).mark_ready_for_decision(approval_id)
        self.approval = Approval.create(
            id=approval_id,
            organization_id=actor.organization_id,
            proposal_id=self.proposal.id,
            proposal_version_number=1,
        )
        self.version = ProposalVersion(
            id=uuid4(),
            organization_id=actor.organization_id,
            proposal_id=self.proposal.id,
            version_number=1,
            created_by_membership_id=actor.membership_id,
            content=_plan(actor.membership_id),
            assumptions=[],
            source_reference_snapshot=[],
            workflow_version="1.0.0",
            prompt_version="planning.v1",
            schema_version="planning-proposal.v1",
            model_reference="mock:base",
            verifier_version="1.0.0",
            creator_type="AI_SYSTEM",
        )
        self.active_memberships = frozenset({actor.membership_id})
        self.finalize_calls = 0
        self.appended: list[ProposalVersion] = []
        self.business_rows = 0
        self.approval_service_calls = 0
        self.source_fresh = True

    async def get_ai_revision_preflight(self, **_):
        if self.proposal.status is not ProposalStatus.READY_FOR_DECISION:
            return None
        return ProposalAIRevisionPreflight(
            run=self.run,
            proposal=self.proposal,
            version=self.version,
            approval=self.approval,
            locale="en",
        )

    async def list_active_membership_ids(self, **_):
        return self.active_memberships

    async def finalize_ai_revision_mutation(self, **values):
        self.finalize_calls += 1
        if (
            not self.source_fresh
            or self.proposal.status is not ProposalStatus.READY_FOR_DECISION
            or self.proposal.current_version_number != values["base_version"]
            or self.approval.status.value != "PENDING"
        ):
            return None
        content = values["content"]
        version = ProposalVersion(
            id=uuid4(),
            organization_id=self.proposal.organization_id,
            proposal_id=self.proposal.id,
            version_number=2,
            created_by_membership_id=values["actor"].membership_id,
            content=content,
            assumptions=[],
            change_summary=values["change_summary"],
            field_provenance={"default": "AI_PROPOSED"},
            validation_result=values["validation_result"],
            source_reference_snapshot=[],
            workflow_version="1.0.0",
            prompt_version="planning.v1",
            schema_version="planning-proposal.v1",
            model_reference=values["model_reference"],
            verifier_version="1.0.0",
            creator_type="AI_SYSTEM",
        )
        self.appended.append(version)
        self.approval = self.approval.mark_superseded()
        self.proposal = replace(
            self.proposal.edit(),
            status=ProposalStatus.DRAFT,
        )
        return version

    async def append_event(self, **_):
        return None


class _CountingTransaction(AbstractAsyncContextManager["_CountingTransaction"]):
    active = 0

    def __init__(self, repository: _RevisionRepository) -> None:
        self.repository = repository

    async def __aenter__(self) -> Self:
        type(self).active += 1
        return self

    async def __aexit__(self, *_):
        type(self).active -= 1

    async def commit(self):
        return None


class _ActorResolver:
    def __init__(self, actor: AuthenticatedActor | None) -> None:
        self.actor = actor

    async def resolve(self, **_):
        return self.actor


class _RevisionGraph:
    def __init__(self, actor: AuthenticatedActor, *, fail: bool = False) -> None:
        self.actor = actor
        self.fail = fail
        self.calls = 0

    async def generate_revision(self, *, base, instruction, context):
        assert _CountingTransaction.active == 0
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider secret must not persist")
        content = PlanningModelOutput.model_validate(
            _plan(self.actor.membership_id, include_new=True)
        )
        return PlanningRevisionDraft(
            base_proposal_id=base.proposal_id,
            base_version=base.version,
            content=content,
            change_summary="Changed dates",
            model_reference="mock:revision",
        )


def _revision_job(repository: _RevisionRepository, actor: AuthenticatedActor) -> WorkflowJob:
    return WorkflowJob(
        id=uuid4(),
        organization_id=actor.organization_id,
        workflow_run_id=repository.run.id,
        job_type="proposal.ai_revise",
        status=WorkflowJobStatus.RUNNING,
        payload={
            "proposal_id": str(repository.proposal.id),
            "base_version": 1,
            "instruction": "Change dates",
            "requester_membership_id": str(actor.membership_id),
        },
    )


def _revision_handler(
    repository: _RevisionRepository,
    actor: AuthenticatedActor,
    graph: _RevisionGraph,
) -> ProposalAIRevisionJobHandler:
    return ProposalAIRevisionJobHandler(
        transaction_factory=lambda _: _CountingTransaction(repository),
        actor_resolver=_ActorResolver(actor),
        graph_factory=lambda _: graph,
        runtime=_Runtime(),
    )


@pytest.mark.asyncio
async def test_ai_revision_model_call_observes_no_active_transaction() -> None:
    actor = _actor()
    repository = _RevisionRepository(actor)
    graph = _RevisionGraph(actor)

    await _revision_handler(repository, actor, graph)(
        job=_revision_job(repository, actor), worker_id="worker"
    )

    assert graph.calls == 1
    assert repository.finalize_calls == 1
    assert _CountingTransaction.active == 0


@pytest.mark.asyncio
async def test_ai_revision_keeps_all_planned_tasks_unassigned() -> None:
    actor = _actor()
    repository = _RevisionRepository(actor)

    await _revision_handler(repository, actor, _RevisionGraph(actor))(
        job=_revision_job(repository, actor), worker_id="worker"
    )

    tasks = repository.appended[0].content["tasks"]
    assert tasks[0]["assignee_membership_id"] is None
    assert tasks[1]["assignee_membership_id"] is None


@pytest.mark.asyncio
async def test_approval_winning_during_revision_causes_zero_revision_state() -> None:
    actor = _actor()
    repository = _RevisionRepository(actor)
    graph = _RevisionGraph(actor)
    generate = graph.generate_revision

    async def approve_during_model(*, base, instruction, context):
        result = await generate(base=base, instruction=instruction, context=context)
        repository.proposal = repository.proposal.mark_approved()
        repository.approval = repository.approval.decide_approve(decided_by=actor.membership_id)
        return result

    graph.generate_revision = approve_during_model  # type: ignore[method-assign]
    await _revision_handler(repository, actor, graph)(
        job=_revision_job(repository, actor), worker_id="worker"
    )

    assert repository.appended == []
    assert repository.proposal.status is ProposalStatus.APPROVED


@pytest.mark.asyncio
async def test_revision_final_transaction_rechecks_source_and_assignee_freshness() -> None:
    actor = _actor()
    repository = _RevisionRepository(actor)
    graph = _RevisionGraph(actor)
    generate = graph.generate_revision

    async def stale_during_model(*, base, instruction, context):
        result = await generate(base=base, instruction=instruction, context=context)
        repository.source_fresh = False
        repository.active_memberships = frozenset()
        return result

    graph.generate_revision = stale_during_model  # type: ignore[method-assign]
    await _revision_handler(repository, actor, graph)(
        job=_revision_job(repository, actor), worker_id="worker"
    )
    assert repository.appended == []
    assert repository.approval.status.value == "PENDING"


@pytest.mark.asyncio
async def test_revision_failure_leaves_original_proposal_and_approval_decidable() -> None:
    actor = _actor()
    repository = _RevisionRepository(actor)

    with pytest.raises(RuntimeError, match="AI_REVISION_MODEL_FAILED"):
        await _revision_handler(repository, actor, _RevisionGraph(actor, fail=True))(
            job=_revision_job(repository, actor), worker_id="worker"
        )

    assert repository.appended == []
    assert repository.proposal.status is ProposalStatus.READY_FOR_DECISION
    assert repository.approval.status.value == "PENDING"


@pytest.mark.asyncio
async def test_revision_never_creates_business_rows_or_calls_approval_service() -> None:
    actor = _actor()
    repository = _RevisionRepository(actor)
    await _revision_handler(repository, actor, _RevisionGraph(actor))(
        job=_revision_job(repository, actor), worker_id="worker"
    )
    assert repository.business_rows == 0
    assert repository.approval_service_calls == 0


@pytest.mark.asyncio
async def test_revision_job_fails_closed_when_current_actor_lost_manager_role() -> None:
    manager = _actor()
    repository = _RevisionRepository(manager)
    employee = AuthenticatedActor(
        user_id=manager.user_id,
        email=manager.email,
        display_name=manager.display_name,
        membership_id=manager.membership_id,
        organization_id=manager.organization_id,
        organization_name=manager.organization_name,
        role=MembershipRole.EMPLOYEE,
    )
    graph = _RevisionGraph(manager)

    with pytest.raises(RuntimeError, match="ACTOR_CONTEXT_UNAVAILABLE"):
        await _revision_handler(repository, employee, graph)(
            job=_revision_job(repository, manager), worker_id="worker"
        )

    assert graph.calls == 0
    assert repository.appended == []
