# pyright: reportUnknownParameterType=false, reportMissingParameterType=false

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.modules.assistant.adapters.work_tools import RecordingToolExecutor, WorkToolExecutor
from app.modules.assistant.domain.models import InvocationStatus, ToolInvocation
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.work.planning.domain.acceptance_criteria import AcceptanceCriterion
from work_management_ai.runtime.contracts import (
    ActorReference,
    ToolExecutionRequest,
    ToolExecutionResult,
)
from work_management_ai.runtime.manifests import ToolManifest, load_yaml_resource
from work_management_ai.runtime.tool_registry import ToolRegistry


class Resolver:
    async def resolve(self, **kwargs):
        raise AssertionError("actor resolution must happen after allowlist validation")


def registry() -> ToolRegistry:
    return ToolRegistry(
        (
            load_yaml_resource(
                "work_management_ai.tools.work.read_my_tasks", "tool.yaml", ToolManifest
            ),
            load_yaml_resource(
                "work_management_ai.tools.work.read_resource", "tool.yaml", ToolManifest
            ),
        )
    )


@pytest.mark.asyncio
async def test_tool_manifest_allowlist_denies_work_mutation_name() -> None:
    executor = WorkToolExecutor(
        actor_resolver=Resolver(),
        tool_registry=registry(),
        task_service=object(),  # type: ignore[arg-type]
        project_service=object(),  # type: ignore[arg-type]
        planning_service=object(),  # type: ignore[arg-type]
    )
    result = await executor.execute(
        ToolExecutionRequest(
            agent_run_id=uuid4(),
            tool_id="work.update_task",
            tool_version="1",
            call_id="call",
            actor=ActorReference(membership_id=uuid4(), organization_id=uuid4()),
            typed_input={},
            idempotency_key="idempotency",
        )
    )
    assert result.status == "REJECTED"
    assert result.safe_error_code == "TOOL_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_tool_registry_accepts_exact_semantic_manifest_version() -> None:
    executor = WorkToolExecutor(
        actor_resolver=Resolver(),
        tool_registry=registry(),
        task_service=object(),  # type: ignore[arg-type]
        project_service=object(),  # type: ignore[arg-type]
        planning_service=object(),  # type: ignore[arg-type]
    )

    with pytest.raises(AssertionError, match="allowlist validation"):
        await executor.execute(
            ToolExecutionRequest(
                agent_run_id=uuid4(),
                tool_id="work.read_my_tasks",
                tool_version="1.0.0",
                call_id="call",
                actor=ActorReference(membership_id=uuid4(), organization_id=uuid4()),
                typed_input={"limit": 10},
                idempotency_key="idempotency",
            )
        )


@pytest.mark.asyncio
async def test_completed_tool_invocation_replays_without_second_service_call() -> None:
    organization_id = uuid4()
    request = ToolExecutionRequest(
        agent_run_id=uuid4(),
        tool_id="work.read_my_tasks",
        tool_version="1.0.0",
        call_id="read:1",
        actor=ActorReference(membership_id=uuid4(), organization_id=organization_id),
        typed_input={"limit": 10},
        idempotency_key="read-once",
    )
    stored = ToolInvocation(
        id=uuid4(),
        organization_id=organization_id,
        agent_run_id=request.agent_run_id,
        tool_id=request.tool_id,
        tool_version=request.tool_version,
        risk_level="READ_ONLY",
        typed_input=request.typed_input,
        typed_output={"resolution": "NOT_FOUND", "evidence": [], "next_task_id": None},
        context_references=(),
        status=InvocationStatus.SUCCEEDED,
        idempotency_key=request.idempotency_key,
        dedupe_key=request.call_id,
        safe_error_code=None,
        completed_at=None,
    )

    class Repo:
        async def get_tool_invocation(self, **_):
            return stored

    class Transaction:
        repository = Repo()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def commit(self):
            return None

    class Backend:
        async def execute(self, _: ToolExecutionRequest):
            raise AssertionError("completed invocation must replay")

    executor = RecordingToolExecutor(
        transaction_factory=lambda _: Transaction(),  # type: ignore[arg-type]
        tool_registry=registry(),
        backend=Backend(),  # type: ignore[arg-type]
    )

    result = await executor.execute(request)

    assert result.status == "SUCCEEDED"
    assert result.typed_output["resolution"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_acceptance_criterion_read_reuses_manual_visibility_service() -> None:
    organization_id, membership_id = uuid4(), uuid4()
    actor = AuthenticatedActor(
        user_id=uuid4(),
        email="employee@example.test",
        display_name="Employee",
        membership_id=membership_id,
        organization_id=organization_id,
        organization_name="Tenant",
        role=MembershipRole.EMPLOYEE,
    )
    criterion = AcceptanceCriterion(
        id=uuid4(),
        organization_id=organization_id,
        task_id=uuid4(),
        text="Result is reviewable",
        position=1,
        version=3,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    class ActorResolver:
        async def resolve(
            self, *, organization_id: UUID, membership_id: UUID
        ) -> AuthenticatedActor | None:
            return actor

    class Planning:
        async def get_acceptance_criterion(
            self, *, actor: AuthenticatedActor, criterion_id: UUID
        ) -> AcceptanceCriterion:
            assert criterion_id == criterion.id
            return criterion

    executor = WorkToolExecutor(
        actor_resolver=ActorResolver(),
        tool_registry=registry(),
        task_service=object(),  # type: ignore[arg-type]
        project_service=object(),  # type: ignore[arg-type]
        planning_service=Planning(),  # type: ignore[arg-type]
    )

    result = await executor.execute(
        ToolExecutionRequest(
            agent_run_id=uuid4(),
            tool_id="work.read_resource",
            tool_version="1.0.0",
            call_id="criterion:1",
            actor=ActorReference(membership_id=membership_id, organization_id=organization_id),
            typed_input={
                "resource_type": "ACCEPTANCE_CRITERION",
                "reference": str(criterion.id),
            },
            idempotency_key="criterion-read",
        )
    )

    assert result.status == "SUCCEEDED"
    assert result.typed_output["resolution"] == "UNIQUE"
    assert result.evidence[0].resource_id == criterion.id


@pytest.mark.asyncio
async def test_schedule_preview_replay_rechecks_authority() -> None:
    organization_id = uuid4()
    request = ToolExecutionRequest(
        agent_run_id=uuid4(),
        tool_id="automation.preview",
        tool_version="1.0.0",
        call_id="read:1",
        actor=ActorReference(membership_id=uuid4(), organization_id=organization_id),
        typed_input={"limit": 10},
        idempotency_key="read-once",
    )
    stored = ToolInvocation(
        id=uuid4(),
        organization_id=organization_id,
        agent_run_id=request.agent_run_id,
        tool_id=request.tool_id,
        tool_version=request.tool_version,
        risk_level="PROPOSAL_ONLY",
        typed_input=request.typed_input,
        typed_output={"resolution": "NOT_FOUND", "evidence": [], "next_task_id": None},
        context_references=(),
        status=InvocationStatus.SUCCEEDED,
        idempotency_key=request.idempotency_key,
        dedupe_key=request.call_id,
        safe_error_code=None,
        completed_at=None,
    )

    class Repo:
        async def get_tool_invocation(self, **_):
            return stored

    class Transaction:
        repository = Repo()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def commit(self):
            return None

    class Backend:
        async def execute(self, _: ToolExecutionRequest):
            return ToolExecutionResult(
                status="REJECTED", typed_output={}, safe_error_code="ACTOR_INACTIVE"
            )

    executor = RecordingToolExecutor(
        transaction_factory=lambda _: Transaction(),  # type: ignore[arg-type]
        tool_registry=ToolRegistry(
            (load_yaml_resource("work_management_ai.tools.automation", "tool.yaml", ToolManifest),)
        ),
        backend=Backend(),  # type: ignore[arg-type]
    )

    result = await executor.execute(request)

    assert result.status == "REJECTED"
    assert result.safe_error_code == "ACTOR_INACTIVE"
