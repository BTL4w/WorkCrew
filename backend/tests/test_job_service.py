from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from app.core.config import Settings
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.planning_runs.adapters.ai_runtime import (
    CurrentActorResolver,
    PlanningJobHandler,
    WorkflowRecordingModelGateway,
)
from app.modules.planning_runs.application.ports import (
    PlanningRunRepository,
    PlanningRunTransaction,
)
from app.modules.planning_runs.domain.models import (
    ModelInvocation,
    OutboxEvent,
    OutboxStatus,
    PlanningRunDomainError,
    WorkflowEvent,
    WorkflowJob,
    WorkflowJobStatus,
    WorkflowRun,
    WorkflowRunStatus,
)
from work_management_ai.model_gateway.contracts import (
    StructuredModelRequest,
    StructuredModelResponse,
)
from work_management_ai.workflows.planning.graph import PlanningGraphResult
from work_management_ai.workflows.planning.state import PlanningState, create_planning_state

_TEST_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")


def pending_job(job_type: str = "planning.start") -> WorkflowJob:
    now = datetime.now(UTC)
    return WorkflowJob(
        id=uuid4(),
        organization_id=_TEST_ORG_ID,
        workflow_run_id=uuid4(),
        job_type=job_type,
        status=WorkflowJobStatus.QUEUED,
        payload={},
        created_at=now,
        updated_at=now,
        available_at=now,
    )


def pending_outbox_event(max_attempts: int = 3) -> OutboxEvent:
    return OutboxEvent(
        id=uuid4(),
        organization_id=_TEST_ORG_ID,
        event_id=uuid4(),
        event_type="test.event",
        aggregate_type="test",
        aggregate_id=uuid4(),
        payload={"foo": "bar"},
        status=OutboxStatus.PENDING,
        attempt_count=0,
        max_attempts=max_attempts,
        last_error_code=None,
        last_error=None,
    )


class FakeJobRepository:
    def __init__(
        self, jobs: list[WorkflowJob] | None = None, outbox_events: list[OutboxEvent] | None = None
    ) -> None:
        self.jobs = jobs or []
        self.outbox_events = outbox_events or []
        self.completed_jobs: set[UUID] = set()
        self.failed_jobs: list[tuple[UUID, str]] = []
        self.published_events: set[UUID] = set()
        self.failed_events: list[tuple[UUID, str]] = []
        self.updated_runs: list[WorkflowRun] = []
        self.workflow_events: list[WorkflowEvent] = []

    async def update_workflow_run(
        self,
        *,
        actor: AuthenticatedActor,
        run: WorkflowRun,
    ) -> WorkflowRun:
        del actor
        self.updated_runs.append(run)
        return run

    async def append_event(
        self,
        *,
        event: WorkflowEvent | None = None,
        actor: AuthenticatedActor | None = None,
        run_id: UUID | None = None,
        event_type: str | None = None,
        public_payload: dict[str, object] | None = None,
    ) -> WorkflowEvent:
        if event is None:
            assert actor is not None
            assert run_id is not None
            assert event_type is not None
            assert public_payload is not None
            event = WorkflowEvent(
                id=uuid4(),
                organization_id=actor.organization_id,
                workflow_run_id=run_id,
                sequence=len(self.workflow_events) + 1,
                event_type=event_type,
                public_payload=public_payload,
            )
        self.workflow_events.append(event)
        return event

    async def claim_job(
        self,
        *,
        organization_id: UUID,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> WorkflowJob | None:
        del now
        for i, job in enumerate(self.jobs):
            if job.organization_id == organization_id and job.status == WorkflowJobStatus.QUEUED:
                job = WorkflowJob(
                    id=job.id,
                    workflow_run_id=job.workflow_run_id,
                    organization_id=job.organization_id,
                    job_type=job.job_type,
                    status=WorkflowJobStatus.RUNNING,
                    created_at=job.created_at,
                    updated_at=job.updated_at,
                    available_at=job.available_at,
                    attempt_count=job.attempt_count + 1,
                    max_attempts=job.max_attempts,
                    last_error=None,
                    payload=job.payload,
                    locked_by_worker_id=worker_id,
                    lease_until=lease_until,
                )
                self.jobs[i] = job
                return job
        return None

    async def complete_job(
        self,
        *,
        job_id: UUID,
        worker_id: str,
    ) -> None:
        self.completed_jobs.add(job_id)

    async def fail_job(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        error_message: str,
        next_available_at: datetime,
    ) -> None:
        self.failed_jobs.append((job_id, error_message))

    async def claim_pending_outbox_events(
        self,
        *,
        organization_id: UUID,
        worker_id: str,
        limit: int,
        now: datetime,
        lease_until: datetime,
    ) -> list[OutboxEvent]:
        claimed: list[OutboxEvent] = []
        for i, event in enumerate(self.outbox_events):
            if event.organization_id == organization_id and event.status == OutboxStatus.PENDING:
                event = OutboxEvent(
                    id=event.id,
                    organization_id=event.organization_id,
                    event_id=event.event_id,
                    event_type=event.event_type,
                    aggregate_type=event.aggregate_type,
                    aggregate_id=event.aggregate_id,
                    payload=event.payload,
                    status=OutboxStatus.DISPATCHING,
                    attempt_count=event.attempt_count + 1,
                    max_attempts=event.max_attempts,
                    last_error_code=None,
                    last_error=None,
                )
                self.outbox_events[i] = event
                claimed.append(event)
                if len(claimed) >= limit:
                    break
        return claimed

    async def mark_outbox_event_published(
        self,
        *,
        organization_id: UUID,
        event_id: UUID,
        worker_id: str,
        now: datetime,
        published_at: datetime,
    ) -> None:
        self.published_events.add(event_id)

    async def record_outbox_event_failure(
        self,
        *,
        organization_id: UUID,
        event_id: UUID,
        worker_id: str,
        now: datetime,
        error_code: str,
        error_message: str,
        next_available_at: datetime,
    ) -> None:
        for i, event in enumerate(self.outbox_events):
            if event.event_id == event_id:
                status = (
                    OutboxStatus.FAILED
                    if event.attempt_count >= event.max_attempts
                    else OutboxStatus.PENDING
                )
                self.outbox_events[i] = OutboxEvent(
                    id=event.id,
                    organization_id=event.organization_id,
                    event_id=event.event_id,
                    event_type=event.event_type,
                    aggregate_type=event.aggregate_type,
                    aggregate_id=event.aggregate_id,
                    payload=event.payload,
                    status=status,
                    available_at=next_available_at,
                    attempt_count=event.attempt_count,
                    max_attempts=event.max_attempts,
                    last_error_code=error_code,
                    last_error=error_message,
                )
        self.failed_events.append((event_id, error_message))

    # Stubs for other methods to satisfy Protocol
    # type: ignore


class FakeJobTransaction(PlanningRunTransaction):
    def __init__(self, repo: FakeJobRepository) -> None:
        self._repo = repo

    @property
    def repository(self) -> PlanningRunRepository:
        return self._repo  # type: ignore

    @property
    def session(self) -> None:
        return None

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass

    async def __aenter__(self) -> "PlanningRunTransaction":
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        pass


class FakeJobTransactionFactory:
    def __init__(self, repo: FakeJobRepository) -> None:
        self.repo = repo
        self.active = 0
        self.commits = 0

    def __call__(self, context: AuthenticatedActor | UUID) -> PlanningRunTransaction:
        factory = self

        class TrackingTransaction(FakeJobTransaction):
            async def __aenter__(self) -> PlanningRunTransaction:
                factory.active += 1
                return await super().__aenter__()

            async def commit(self) -> None:
                factory.commits += 1

            async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
                factory.active -= 1

        return TrackingTransaction(self.repo)


def test_compute_backoff_seconds_cap() -> None:
    from app.modules.planning_runs.application.job_service import compute_backoff_seconds

    assert compute_backoff_seconds(0) == 0
    assert compute_backoff_seconds(1) == 5
    assert compute_backoff_seconds(2) == 10
    assert compute_backoff_seconds(3) == 20
    assert compute_backoff_seconds(6) == 160
    assert compute_backoff_seconds(7) == 300
    assert compute_backoff_seconds(10) == 300
    assert compute_backoff_seconds(20) == 300


@pytest.mark.asyncio
async def test_planning_job_records_terminal_failure_event_for_manual_fallback() -> None:
    actor = AuthenticatedActor(
        user_id=uuid4(),
        email="manager@example.com",
        display_name="Manager",
        membership_id=uuid4(),
        organization_id=_TEST_ORG_ID,
        organization_name="Test",
        role=MembershipRole.MANAGER,
    )
    run = WorkflowRun.create(
        organization_id=actor.organization_id,
        project_id=None,
        requested_by_membership_id=actor.membership_id,
        workflow_name="planning",
        workflow_version="2.0.0",
        verifier_version="2.0.0",
        input_goal_text="Lập kế hoạch",
    ).mark_running()
    state = create_planning_state(
        run_id=run.id,
        organization_id=run.organization_id,
        actor_membership_id=actor.membership_id,
        actor_role=actor.role.value,
        locale="vi",
        user_brief=run.input_goal_text,
    )
    failed_state = dict(state)
    failed_state["stage"] = "MANUAL_FALLBACK"
    repository = FakeJobRepository()
    handler = PlanningJobHandler(
        Settings(environment="test"),
        FakeJobTransactionFactory(repository),
        cast(CurrentActorResolver, object()),
    )

    await handler._apply_result(  # pyright: ignore[reportPrivateUsage]
        actor,
        run,
        PlanningGraphResult(state=cast(PlanningState, failed_state), interrupt=None),
    )

    assert repository.updated_runs[-1].status is WorkflowRunStatus.FAILED
    assert [event.event_type for event in repository.workflow_events] == ["workflow.failed"]
    assert repository.workflow_events[0].public_payload == {
        "safe_error_code": "AI_WORKFLOW_UNAVAILABLE",
        "stage": "MANUAL_FALLBACK",
    }


@pytest.mark.asyncio
async def test_job_service_rejects_unconfigured_tenant() -> None:
    from app.modules.planning_runs.application.job_service import JobService

    factory = FakeJobTransactionFactory(FakeJobRepository())
    service = JobService(
        transaction_factory=factory,
        handlers={},
        organization_scopes={uuid4()},
    )
    with pytest.raises(PlanningRunDomainError, match="Worker organization scope violation"):
        await service.run_once(worker_id="w-1", organization_id=uuid4())


@pytest.mark.asyncio
async def test_outbox_service_rejects_unconfigured_tenant() -> None:
    from app.modules.planning_runs.application.outbox_service import (
        OutboxService,
        UnsupportedOutboxPublisher,
    )

    factory = FakeJobTransactionFactory(FakeJobRepository())
    service = OutboxService(
        transaction_factory=factory,
        publisher=UnsupportedOutboxPublisher(),
        organization_scopes={uuid4()},
    )
    with pytest.raises(PlanningRunDomainError, match="Worker organization scope violation"):
        await service.dispatch_once(worker_id="w-1", organization_id=uuid4())


@pytest.mark.asyncio
async def test_unsupported_outbox_publisher_transitions_to_failed() -> None:
    event = pending_outbox_event(max_attempts=1)
    repo = FakeJobRepository(outbox_events=[event])
    factory = FakeJobTransactionFactory(repo)

    from app.modules.planning_runs.application.outbox_service import (
        OutboxService,
        UnsupportedOutboxPublisher,
    )

    service = OutboxService(
        transaction_factory=factory,
        publisher=UnsupportedOutboxPublisher(),
        organization_scopes={_TEST_ORG_ID},
    )
    result = await service.dispatch_once(worker_id="w-1", organization_id=_TEST_ORG_ID)

    assert result is True
    # Should transition to FAILED due to max_attempts=1 and NotImplementedError
    updated = repo.outbox_events[0]
    assert updated.status == OutboxStatus.FAILED


@pytest.mark.asyncio
async def test_job_service_custom_lease_seconds() -> None:
    from app.modules.planning_runs.application.job_service import JobService

    repo = FakeJobRepository()
    factory = FakeJobTransactionFactory(repo)
    service = JobService(
        transaction_factory=factory,
        handlers={},
        organization_scopes={_TEST_ORG_ID},
        lease_seconds=120,
    )
    assert service.lease_seconds == 120


@pytest.mark.asyncio
async def test_planning_job_claim_commits_before_handler_and_execution_has_no_transaction() -> None:
    from app.modules.planning_runs.application.job_service import JobService

    job = pending_job()
    repo = FakeJobRepository(jobs=[job])
    factory = FakeJobTransactionFactory(repo)
    observed: list[tuple[int, int]] = []

    async def handler(*, job: WorkflowJob, worker_id: str) -> None:
        del job, worker_id
        observed.append((factory.active, factory.commits))

    service = JobService(
        transaction_factory=factory,
        handlers={"planning.start": handler},
        organization_scopes={_TEST_ORG_ID},
    )

    assert await service.run_once(worker_id="w-1", organization_id=_TEST_ORG_ID)
    assert observed == [(0, 1)]
    assert repo.completed_jobs == {job.id}
    assert factory.commits == 2


@pytest.mark.asyncio
async def test_planning_job_failure_persists_only_safe_error_code() -> None:
    from app.modules.planning_runs.application.job_service import JobService

    job = pending_job()
    repo = FakeJobRepository(jobs=[job])

    async def handler(*, job: WorkflowJob, worker_id: str) -> None:
        del job, worker_id
        raise RuntimeError("sk-secret raw provider failure")

    service = JobService(
        transaction_factory=FakeJobTransactionFactory(repo),
        handlers={"planning.start": handler},
        organization_scopes={_TEST_ORG_ID},
    )

    assert await service.run_once(worker_id="w-1", organization_id=_TEST_ORG_ID)
    assert repo.failed_jobs == [(job.id, "PLANNING_JOB_FAILED")]


@pytest.mark.asyncio
async def test_planning_model_call_has_no_active_transaction_and_records_no_prompt() -> None:
    workflow_run_id = uuid4()

    class Output(BaseModel):
        answer: str

    class Repository:
        invocation: ModelInvocation | None = None

        async def assert_execution_context(self, **_: object) -> None:
            return None

        async def record_model_invocation(self, *, invocation: ModelInvocation) -> None:
            self.invocation = invocation

    repository = Repository()

    class Transaction:
        active = False

        async def __aenter__(self) -> "Transaction":
            self.active = True
            return self

        async def __aexit__(self, *_: object) -> None:
            self.active = False

        @property
        def repository(self) -> Repository:
            return repository

        async def commit(self) -> None:
            self.active = False

    transaction = Transaction()

    class Gateway:
        async def generate_structured[OutputT: BaseModel](
            self, request: StructuredModelRequest[OutputT]
        ) -> StructuredModelResponse[OutputT]:
            assert transaction.active is False
            return StructuredModelResponse(
                parsed=request.output_schema.model_validate({"answer": "safe"}),
                model_ref="mock:planning-v1",
            )

    gateway = WorkflowRecordingModelGateway(
        gateway=Gateway(),
        transaction_factory=lambda _: transaction,  # type: ignore[arg-type]
        organization_id=_TEST_ORG_ID,
        workflow_run_id=workflow_run_id,
    )

    await gateway.generate_structured(
        StructuredModelRequest(
            invocation_key="planning.en.generate",
            messages=(),
            output_schema=Output,
            timeout_seconds=5,
        )
    )

    recorded = repository.invocation
    assert recorded is not None
    assert recorded.workflow_run_id == workflow_run_id
    assert not hasattr(recorded, "messages")
