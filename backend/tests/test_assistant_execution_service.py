"""Current-actor guard tests for Assistant execution."""

from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from app.modules.assistant.adapters.agent_runtime import (
    AgentRecordingModelGateway,
    AssistantTurnExecutor,
    agent_model_scope,
    build_agent_registry,
    resolve_ambient_planning_context,
)
from app.modules.assistant.application.execution_service import (
    AssistantExecutionError,
    AssistantExecutionService,
)
from app.modules.assistant.domain.models import (
    AgentModelInvocation,
    AssistantJob,
    AssistantMessage,
    MessageRole,
    OrchestrationRun,
)
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from work_management_ai.model_gateway.contracts import (
    StructuredModelRequest,
    StructuredModelResponse,
)
from work_management_ai.model_gateway.errors import (
    ModelInvalidOutputError,
    ModelRateLimitError,
    ModelTimeoutError,
    ModelUnavailableError,
)


def _job() -> AssistantJob:
    return AssistantJob.create(
        organization_id=uuid4(),
        conversation_id=uuid4(),
        turn_id=uuid4(),
        orchestration_run_id=uuid4(),
        requester_membership_id=uuid4(),
        payload={},
    )


def test_latest_current_proposal_is_available_as_ambient_chat_context() -> None:
    organization_id = uuid4()
    conversation_id = uuid4()
    workflow_run_id = uuid4()
    proposal_id = uuid4()
    messages = (
        AssistantMessage(
            id=uuid4(),
            organization_id=organization_id,
            conversation_id=conversation_id,
            sequence=1,
            role=MessageRole.ASSISTANT,
            content_blocks=(
                {
                    "kind": "proposal",
                    "workflow_run_id": str(workflow_run_id),
                    "proposal_id": str(proposal_id),
                    "proposal_version": 1,
                    "state": "SUPERSEDED",
                    "read_only": True,
                    "current_version": 2,
                },
            ),
        ),
        AssistantMessage(
            id=uuid4(),
            organization_id=organization_id,
            conversation_id=conversation_id,
            sequence=2,
            role=MessageRole.ASSISTANT,
            content_blocks=(
                {
                    "kind": "proposal",
                    "workflow_run_id": str(workflow_run_id),
                    "proposal_id": str(proposal_id),
                    "proposal_version": 2,
                    "state": "READY_FOR_DECISION",
                    "read_only": False,
                },
            ),
        ),
    )
    context = resolve_ambient_planning_context(messages)

    assert context is not None
    assert context.model_dump(mode="json") == {
        "workflow_run_id": str(workflow_run_id),
        "workflow_status": "WAITING_FOR_DECISION",
        "proposal_id": str(proposal_id),
        "proposal_version": 2,
        "proposal_status": "READY_FOR_DECISION",
        "requested_operation": None,
    }


@pytest.mark.asyncio
async def test_worker_re_resolves_current_role_before_runtime_call() -> None:
    job = _job()
    actor = AuthenticatedActor(
        user_id=uuid4(),
        email="employee@example.test",
        display_name="Employee",
        membership_id=job.requester_membership_id,
        organization_id=job.organization_id,
        organization_name="Tenant",
        role=MembershipRole.EMPLOYEE,
    )

    class Resolver:
        async def resolve(
            self, *, organization_id: UUID, membership_id: UUID
        ) -> AuthenticatedActor | None:
            assert (organization_id, membership_id) == (
                job.organization_id,
                job.requester_membership_id,
            )
            return actor

    class Runtime:
        seen: AuthenticatedActor | None = None

        async def execute_job(self, *, job: AssistantJob, actor: AuthenticatedActor) -> None:
            self.seen = actor

    runtime = Runtime()
    await AssistantExecutionService(actor_resolver=Resolver(), runtime=runtime).execute(
        job=job, worker_id="worker"
    )

    assert runtime.seen is actor
    assert runtime.seen is not None
    assert runtime.seen.role is MembershipRole.EMPLOYEE


@pytest.mark.asyncio
async def test_deactivated_actor_fails_before_runtime_call() -> None:
    job = _job()

    class Resolver:
        async def resolve(
            self, *, organization_id: UUID, membership_id: UUID
        ) -> AuthenticatedActor | None:
            return None

    class Runtime:
        async def execute_job(self, *, job: AssistantJob, actor: AuthenticatedActor) -> None:
            raise AssertionError("runtime must not execute")

    with pytest.raises(AssistantExecutionError, match="ACTOR_CONTEXT_UNAVAILABLE"):
        await AssistantExecutionService(actor_resolver=Resolver(), runtime=Runtime()).execute(
            job=job, worker_id="worker"
        )


@pytest.mark.asyncio
async def test_model_gateway_records_safe_metadata_after_transaction_free_call() -> None:
    organization_id, agent_run_id = uuid4(), uuid4()

    class Output(BaseModel):
        answer: str

    class Repo:
        recorded: AgentModelInvocation | None = None

        async def get_agent_run(self, **_: object) -> None:
            return None

        async def append_agent_model_invocation(self, *, invocation: AgentModelInvocation) -> None:
            self.recorded = invocation

    repo = Repo()

    class Transaction:
        active = False
        repository = repo

        async def __aenter__(self):
            self.active = True
            return self

        async def __aexit__(self, *_):
            self.active = False
            return None

        async def commit(self):
            self.active = False

    transaction = Transaction()

    class Gateway:
        async def generate_structured[OutputT: BaseModel](
            self, request: StructuredModelRequest[OutputT]
        ) -> StructuredModelResponse[OutputT]:
            assert transaction.active is False
            return StructuredModelResponse(
                parsed=request.output_schema.model_validate({"answer": "safe"}),
                model_ref="mock:model-v1",
            )

    gateway = AgentRecordingModelGateway(
        gateway=Gateway(),  # type: ignore[arg-type]
        transaction_factory=lambda _: transaction,  # type: ignore[arg-type]
    )
    request = StructuredModelRequest(
        invocation_key="work_intelligence.en.plan",
        messages=(),
        output_schema=Output,
        timeout_seconds=5,
    )

    with agent_model_scope(organization_id, agent_run_id):
        response = await gateway.generate_structured(request)

    assert response.parsed.answer == "safe"
    recorded = repo.recorded
    assert recorded is not None
    assert recorded.agent_run_id == agent_run_id
    assert recorded.invocation_key == request.invocation_key
    assert not hasattr(recorded, "messages")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_error", "safe_error_code"),
    [
        (ModelInvalidOutputError("private invalid output"), "MODEL_INVALID_OUTPUT"),
        (ModelTimeoutError("private timeout"), "MODEL_TIMEOUT"),
        (ModelRateLimitError("private rate limit"), "MODEL_RATE_LIMITED"),
        (ModelUnavailableError("private unavailable"), "MODEL_UNAVAILABLE"),
    ],
)
async def test_model_gateway_records_specific_safe_failure_kind(
    provider_error: Exception,
    safe_error_code: str,
) -> None:
    organization_id, agent_run_id = uuid4(), uuid4()

    class Output(BaseModel):
        answer: str

    class Repo:
        recorded: AgentModelInvocation | None = None

        async def get_agent_run(self, **_: object) -> None:
            return None

        async def append_agent_model_invocation(self, *, invocation: AgentModelInvocation) -> None:
            self.recorded = invocation

    repo = Repo()

    class Transaction:
        repository = repo

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def commit(self):
            return None

    class Gateway:
        async def generate_structured[OutputT: BaseModel](
            self, request: StructuredModelRequest[OutputT]
        ) -> StructuredModelResponse[OutputT]:
            del request
            raise provider_error

    gateway = AgentRecordingModelGateway(
        gateway=Gateway(),  # type: ignore[arg-type]
        transaction_factory=lambda _: Transaction(),  # type: ignore[arg-type]
    )
    request = StructuredModelRequest(
        invocation_key="planning_agent.vi.step_plan",
        messages=(),
        output_schema=Output,
        timeout_seconds=5,
    )

    with agent_model_scope(organization_id, agent_run_id), pytest.raises(type(provider_error)):
        await gateway.generate_structured(request)

    assert repo.recorded is not None
    assert repo.recorded.safe_error_code == safe_error_code


@pytest.mark.asyncio
async def test_completed_orchestration_retry_skips_engine_and_all_side_effects() -> None:
    job = _job()
    actor = AuthenticatedActor(
        user_id=uuid4(),
        email="manager@example.test",
        display_name="Manager",
        membership_id=job.requester_membership_id,
        organization_id=job.organization_id,
        organization_name="Tenant",
        role=MembershipRole.MANAGER,
    )
    run = (
        OrchestrationRun.create(
            id=job.orchestration_run_id,
            organization_id=job.organization_id,
            turn_id=job.turn_id,
            orchestrator_version="1.0.0",
            orchestrator_fingerprint="fingerprint",
            execution_plan={"steps": []},
            budget={"max_iterations": 8},
        )
        .mark_running()
        .mark_completed("COMPLETED")
    )

    class Repo:
        async def begin_orchestration(self, *, job: AssistantJob) -> OrchestrationRun:
            return run

        async def get_conversation_snapshot(self, **_: object) -> None:
            return None

    class Transaction:
        repository = Repo()

        async def __aenter__(self) -> "Transaction":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def commit(self) -> None:
            return None

    registry, _ = build_agent_registry()
    executor = AssistantTurnExecutor(
        transaction_factory=lambda _: Transaction(),  # type: ignore[arg-type]
        registry=registry,
        engine_factory=lambda _: (_ for _ in ()).throw(  # type: ignore[arg-type]
            AssertionError("completed run must not execute again")
        ),
    )

    await executor.execute_job(job=job, actor=actor)
