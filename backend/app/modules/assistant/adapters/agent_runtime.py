"""Durable backend boundary for the provider-neutral Agent Runtime."""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic
from typing import Any, Literal, Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel

from app.modules.assistant.adapters.daily_update_tools import DailyUpdateContextResolver
from app.modules.assistant.application.ports import AssistantTransactionFactory
from app.modules.assistant.domain.models import (
    AgentCheckpoint,
    AgentHandoffRecord,
    AgentModelInvocation,
    AgentRun,
    AssistantJob,
    AssistantMessage,
    InvocationStatus,
    OrchestrationRunStatus,
)
from app.modules.assistant.domain.models import (
    AgentRunStatus as DomainAgentRunStatus,
)
from app.modules.identity.domain.auth import AuthenticatedActor
from work_management_ai.agents.assignment.harness import AssignmentAgentHarness
from work_management_ai.agents.daily_update.harness import DailyUpdateHarness
from work_management_ai.agents.orchestrator.contracts import (
    ActiveConversationContext,
    ActivePlanningContext,
    ActiveTeamContext,
    ActorContextResolverPort,
    ConversationExcerpt,
    ExactAssignmentResolution,
    OrchestratorInput,
    OrchestratorOutput,
    PendingFollowup,
)
from work_management_ai.agents.orchestrator.harness import OrchestratorHarness
from work_management_ai.agents.planning.harness import PlanningAgentHarness
from work_management_ai.agents.risk.harness import RiskHarness
from work_management_ai.agents.work_intelligence.harness import WorkIntelligenceHarness
from work_management_ai.model_gateway.contracts import (
    ModelGateway,
    StructuredModelRequest,
    StructuredModelResponse,
)
from work_management_ai.model_gateway.errors import (
    ModelInvalidOutputError,
    ModelRateLimitError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from work_management_ai.runtime.agent_registry import AgentRegistry
from work_management_ai.runtime.budgeted_gateway import BudgetedDailyGateway
from work_management_ai.runtime.contracts import (
    ActivityResponseBlock,
    ActorReference,
    AgentHandoff,
    AgentHarness,
    AgentId,
    AgentResult,
    AgentRunStatus,
    ResolvedActorContext,
    ResponseBlock,
    ToolExecutionRequest,
    ToolExecutionResult,
    ToolExecutorPort,
)
from work_management_ai.runtime.daily_update_budget import (
    MODEL_ATTEMPT_SCOPE,
    BudgetScope,
    UsageStore,
    daily_model_scope,
)
from work_management_ai.runtime.execution_engine import (
    AgentExecutionEngine,
    DurableSpecialistRunner,
    ExecutionCheckpoint,
    ExecutionRecorderPort,
    RecordedAgentRun,
)
from work_management_ai.runtime.manifests import SkillManifest, ToolManifest, load_yaml_resource
from work_management_ai.runtime.policy_guard import PolicyGuard
from work_management_ai.runtime.skill_registry import SkillRegistry
from work_management_ai.runtime.tool_registry import ToolRegistry

_SKILL_RESOURCES = (
    ("work_management_ai.skills.explain_verified_risk", "skill.yaml"),
    ("work_management_ai.skills.review_evidence_concerns", "skill.yaml"),
    ("work_management_ai.skills.extract_daily_update", "skill.yaml"),
    ("work_management_ai.skills.compare_daily_update_evidence", "skill.yaml"),
    ("work_management_ai.skills.answer_work_question", "skill.yaml"),
    ("work_management_ai.skills.create_project_plan", "skill.yaml"),
    ("work_management_ai.skills.revise_project_plan", "skill.yaml"),
    ("work_management_ai.skills.recommend_project_team", "skill.yaml"),
    ("work_management_ai.skills.analyze_workload", "skill.yaml"),
)
_TOOL_RESOURCES = (
    ("work_management_ai.tools.automation", "tool.yaml"),
    ("work_management_ai.tools.risk", "tool.yaml"),
    ("work_management_ai.tools.daily_update", "tool.yaml"),
    ("work_management_ai.tools.work.read_my_tasks", "tool.yaml"),
    ("work_management_ai.tools.work.read_resource", "tool.yaml"),
    ("work_management_ai.tools.planning.manage_run", "tool.yaml"),
    ("work_management_ai.tools.assignment.manage_team", "tool.yaml"),
    ("work_management_ai.tools.assignment.read_workload", "tool.yaml"),
    ("work_management_ai.tools.assignment.assign_task", "tool.yaml"),
)
_AGENT_RESOURCES = (
    ("work_management_ai.agents.risk", "agent.yaml"),
    ("work_management_ai.agents.daily_update", "agent.yaml"),
    ("work_management_ai.agents.orchestrator", "agent.yaml"),
    ("work_management_ai.agents.work_intelligence", "agent.yaml"),
    ("work_management_ai.agents.planning", "agent.yaml"),
    ("work_management_ai.agents.assignment", "agent.yaml"),
)
_EVALUATORS = frozenset(
    {
        "orchestrator_plan@1",
        "risk_grounding@1",
        "daily_update_grounding@1",
        "work_grounding@1",
        "planning_schema@1",
        "planning_invariants@1",
        "planning_grounding@1",
        "assignment_explanation@1",
        "assignment_policy@1",
    }
)
_MODEL_SCOPE: ContextVar[tuple[UUID, UUID] | None] = ContextVar(
    "assistant_agent_model_scope", default=None
)


class CurrentActorResolverPort(Protocol):
    async def resolve(
        self, *, organization_id: UUID, membership_id: UUID
    ) -> AuthenticatedActor | None: ...


class AssignmentContextResolverPort(Protocol):
    async def resolve_assignment(
        self, *, actor: AuthenticatedActor, message: str
    ) -> ExactAssignmentResolution: ...


def resolve_ambient_planning_context(
    messages: tuple[AssistantMessage, ...],
) -> ActivePlanningContext | None:
    """Resolve the latest current proposal without inferring a user operation."""

    closed_workflows: set[UUID] = set()
    for message in reversed(messages):
        for block in reversed(message.content_blocks):
            kind = block.get("kind")
            try:
                workflow_run_id = UUID(str(block["workflow_run_id"]))
            except (KeyError, ValueError):
                continue
            if kind == "decision_result":
                closed_workflows.add(workflow_run_id)
                continue
            if kind != "proposal" or workflow_run_id in closed_workflows:
                continue
            state = block.get("state")
            if block.get("read_only") is True or state not in {
                "READY_FOR_DECISION",
                "VALIDATION_FAILED",
            }:
                continue
            try:
                proposal_id = UUID(str(block["proposal_id"]))
                proposal_version = int(block["proposal_version"])
            except (KeyError, TypeError, ValueError):
                continue
            if proposal_version < 1:
                continue
            return ActivePlanningContext(
                workflow_run_id=workflow_run_id,
                workflow_status="WAITING_FOR_DECISION",
                proposal_id=proposal_id,
                proposal_version=proposal_version,
                proposal_status=str(state),
                requested_operation=None,
            )
    return None


def resolve_ambient_team_context(
    messages: tuple[AssistantMessage, ...],
    *,
    pending_followup: PendingFollowup | None = None,
) -> ActiveTeamContext | None:
    for message in reversed(messages):
        for block in reversed(message.content_blocks):
            if (
                block.get("kind") == "decision_result"
                and block.get("decision") == "APPROVE"
                and block.get("project_id") is not None
                and block.get("continue_team") is True
                and pending_followup is not None
                and pending_followup.state == "READY"
            ):
                try:
                    if (
                        UUID(str(block["workflow_run_id"]))
                        != pending_followup.planning_workflow_run_id
                        or UUID(str(block["project_id"])) != pending_followup.project_id
                    ):
                        continue
                    return ActiveTeamContext(
                        project_id=UUID(str(pending_followup.project_id)),
                        planning_proposal_id=pending_followup.planning_proposal_id,
                        planning_proposal_version=pending_followup.planning_proposal_version,
                        requested_operation="RECOMMEND_TEAM",
                    )
                except ValueError:
                    continue
            if block.get("kind") != "team_recommendation":
                continue
            try:
                return ActiveTeamContext(
                    project_id=UUID(str(block["project_id"])),
                    recommendation_id=UUID(str(block["recommendation_id"])),
                    recommendation_version=int(block["recommendation_version"]),
                    recommendation_status=str(block["status"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
    return None


@contextmanager
def agent_model_scope(organization_id: UUID, agent_run_id: UUID) -> Generator[None]:
    """Bind safe tenant/run identity to model metadata recording for one Agent call."""
    token = _MODEL_SCOPE.set((organization_id, agent_run_id))
    try:
        yield
    finally:
        _MODEL_SCOPE.reset(token)


class AgentRecordingModelGateway:
    """Call the provider outside transactions, then persist allowlisted metadata."""

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        transaction_factory: AssistantTransactionFactory,
    ) -> None:
        self._gateway = gateway
        self._transactions = transaction_factory

    async def generate_structured[StructuredOutputT: BaseModel](
        self, request: StructuredModelRequest[StructuredOutputT]
    ) -> StructuredModelResponse[StructuredOutputT]:
        scope = _MODEL_SCOPE.get()
        if scope is None:
            raise RuntimeError("AGENT_MODEL_SCOPE_MISSING")
        organization_id, agent_run_id = scope
        started = monotonic()
        status = InvocationStatus.SUCCEEDED
        safe_error_code = None
        model_ref = "unknown:unavailable"
        try:
            response = await self._gateway.generate_structured(request)
            model_ref = response.model_ref
        except Exception as error:
            status = InvocationStatus.FAILED
            safe_error_code = _safe_model_error_code(error)
            raise
        finally:
            provider, separator, model = model_ref.partition(":")
            invocation = AgentModelInvocation(
                id=uuid5(
                    NAMESPACE_URL,
                    (
                        f"agent-model:{agent_run_id}:{request.invocation_key}:{MODEL_ATTEMPT_SCOPE.get()}"
                        if MODEL_ATTEMPT_SCOPE.get() is not None
                        else f"agent-model:{agent_run_id}:{request.invocation_key}"
                    ),
                ),
                organization_id=organization_id,
                agent_run_id=agent_run_id,
                provider=provider or "unknown",
                model=model if separator else "unavailable",
                prompt_version=request.invocation_key[:64],
                schema_version="1.0",
                invocation_key=(
                    f"{request.invocation_key}:attempt:{MODEL_ATTEMPT_SCOPE.get()}"
                    if MODEL_ATTEMPT_SCOPE.get() is not None
                    else request.invocation_key
                ),
                status=status,
                duration_ms=max(0, int((monotonic() - started) * 1000)),
                safe_error_code=safe_error_code,
            )
            async with self._transactions(organization_id) as transaction:
                await transaction.repository.append_agent_model_invocation(invocation=invocation)
                await transaction.commit()
        return response


def _safe_model_error_code(error: Exception) -> str:
    if isinstance(error, ModelInvalidOutputError):
        return "MODEL_INVALID_OUTPUT"
    if isinstance(error, ModelTimeoutError):
        return "MODEL_TIMEOUT"
    if isinstance(error, ModelRateLimitError):
        return "MODEL_RATE_LIMITED"
    if isinstance(error, ModelUnavailableError):
        return "MODEL_UNAVAILABLE"
    return "MODEL_GATEWAY_FAILED"


class _ScopedAgentHarness:
    def __init__(self, harness: AgentHarness, usage_store: UsageStore | None = None) -> None:
        self._harness = harness
        self._usage_store = usage_store

    async def run(self, handoff: AgentHandoff) -> AgentResult:
        run_id = uuid5(NAMESPACE_URL, f"agent-run:{handoff.idempotency_key}")
        with agent_model_scope(handoff.actor.organization_id, run_id):
            if handoff.target_agent_id in {AgentId.DAILY_UPDATE, AgentId.RISK}:
                scope = BudgetScope(
                    organization_id=handoff.actor.organization_id,
                    membership_id=handoff.actor.membership_id,
                    run_id=run_id,
                    max_model_attempts=handoff.budget.max_model_attempts,
                    timeout_seconds=handoff.budget.timeout_seconds,
                )
                with daily_model_scope(scope):
                    result = await self._harness.run(handoff)
                if self._usage_store is not None:
                    result = result.model_copy(
                        update={"model_attempts_used": await self._usage_store.run_attempts(scope)}
                    )
                return result
            return await self._harness.run(handoff)


class _ScopedOrchestrator:
    def __init__(self, harness: OrchestratorHarness) -> None:
        self._harness = harness

    async def run_turn(self, value: OrchestratorInput) -> OrchestratorOutput:
        run_id = uuid5(NAMESPACE_URL, f"orchestrator:{value.turn_id}")
        with agent_model_scope(value.actor.organization_id, run_id):
            return await self._harness.run_turn(value)


def build_agent_registry() -> tuple[AgentRegistry, ToolRegistry]:
    """Load and validate every Phase-3 activated runtime resource at startup."""
    skill_registry = SkillRegistry(
        load_yaml_resource(package, resource, SkillManifest)
        for package, resource in _SKILL_RESOURCES
    )
    tool_registry = ToolRegistry(
        load_yaml_resource(package, resource, ToolManifest) for package, resource in _TOOL_RESOURCES
    )
    registry = AgentRegistry(
        skill_registry=skill_registry,
        tool_registry=tool_registry,
        evaluator_ids=_EVALUATORS,
    )
    for package, resource in _AGENT_RESOURCES:
        registry.register_resource(package, resource)
    return registry, tool_registry


class CurrentAgentActorResolver(ActorContextResolverPort):
    """Translate durable identity state to the provider-neutral Agent contract."""

    def __init__(self, resolver: CurrentActorResolverPort) -> None:
        self._resolver = resolver

    async def resolve(self, reference: ActorReference) -> ResolvedActorContext:
        actor = await self._resolver.resolve(
            organization_id=reference.organization_id,
            membership_id=reference.membership_id,
        )
        if actor is None:
            return ResolvedActorContext(
                membership_id=reference.membership_id,
                organization_id=reference.organization_id,
                role="EMPLOYEE",
                is_active=False,
            )
        return ResolvedActorContext(
            membership_id=actor.membership_id,
            organization_id=actor.organization_id,
            role=actor.role.value,
            is_active=True,
        )


class InactivePlanningToolExecutor(ToolExecutorPort):
    """Fail closed until Task 8 activates the focused Planning bridge."""

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        del request
        return ToolExecutionResult(
            status="REJECTED",
            typed_output={},
            safe_error_code="PLANNING_TOOL_BRIDGE_NOT_ACTIVE",
        )


class InactiveAssignmentToolExecutor(ToolExecutorPort):
    """Fail closed when a test composition omits the Phase 3 application bridge."""

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        del request
        return ToolExecutionResult(
            status="REJECTED",
            typed_output={},
            safe_error_code="ASSIGNMENT_TOOL_BRIDGE_NOT_ACTIVE",
        )


def build_execution_engine_factory(
    *,
    model_gateway: ModelGateway,
    registry: AgentRegistry,
    actor_resolver: CurrentActorResolverPort,
    work_tool_executor: ToolExecutorPort,
    transaction_factory: AssistantTransactionFactory,
    planning_tool_executor: ToolExecutorPort | None = None,
    assignment_tool_executor: ToolExecutorPort | None = None,
    daily_update_tool_executor: ToolExecutorPort | None = None,
    risk_tool_executor: ToolExecutorPort | None = None,
    automation_tool_executor: ToolExecutorPort | None = None,
    daily_usage_store: UsageStore | None = None,
    daily_image_token_bound: int | None = None,
) -> Callable[[ExecutionRecorderPort], AgentExecutionEngine]:
    """Compose hub-and-spoke Harnesses without opening a database transaction."""
    agent_actor_resolver = CurrentAgentActorResolver(actor_resolver)
    recording_gateway = AgentRecordingModelGateway(
        gateway=model_gateway,
        transaction_factory=transaction_factory,
    )

    class ScopedReadTools:
        async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
            if request.tool_id == "risk.read" and risk_tool_executor is not None:
                return await risk_tool_executor.execute(request)
            return await work_tool_executor.execute(request)

    harnesses: Mapping[AgentId, AgentHarness] = {
        AgentId.WORK_INTELLIGENCE: _ScopedAgentHarness(
            WorkIntelligenceHarness(
                model_gateway=recording_gateway,
                tool_executor=ScopedReadTools(),
            )
        ),
        AgentId.PLANNING: _ScopedAgentHarness(
            PlanningAgentHarness(
                model_gateway=recording_gateway,
                tool_executor=planning_tool_executor or InactivePlanningToolExecutor(),
                actor_resolver=agent_actor_resolver,
            )
        ),
        AgentId.ASSIGNMENT: _ScopedAgentHarness(
            AssignmentAgentHarness(
                model_gateway=recording_gateway,
                tool_executor=assignment_tool_executor or InactiveAssignmentToolExecutor(),
                actor_resolver=agent_actor_resolver,
            )
        ),
    }

    if risk_tool_executor is not None:
        harnesses = {
            **harnesses,
            AgentId.RISK: _ScopedAgentHarness(
                RiskHarness(
                    model_gateway=BudgetedDailyGateway(recording_gateway, daily_usage_store)
                    if daily_usage_store is not None
                    else recording_gateway,
                    tool_executor=risk_tool_executor,
                    actor_resolver=agent_actor_resolver,
                ),
                usage_store=daily_usage_store,
            ),
        }

    if daily_update_tool_executor is not None and daily_usage_store is not None:
        harnesses = {
            **harnesses,
            AgentId.DAILY_UPDATE: _ScopedAgentHarness(
                DailyUpdateHarness(
                    model_gateway=BudgetedDailyGateway(
                        recording_gateway,
                        daily_usage_store,
                        image_token_bound=daily_image_token_bound,
                    ),
                    tool_executor=daily_update_tool_executor,
                    actor_resolver=agent_actor_resolver,
                ),
                usage_store=daily_usage_store,
            ),
        }

    def factory(recorder: ExecutionRecorderPort) -> AgentExecutionEngine:
        specialists = DurableSpecialistRunner(recorder=recorder, harnesses=harnesses)
        orchestrator = _ScopedOrchestrator(
            OrchestratorHarness(
                model_gateway=recording_gateway,
                registry=registry,
                policy_guard=PolicyGuard(),
                actor_resolver=agent_actor_resolver,
                specialists=specialists,
                automation_tools=automation_tool_executor,
            )
        )
        return AgentExecutionEngine(orchestrator)

    return factory


class AgentJobExecutor(Protocol):
    async def execute_job(self, *, job: AssistantJob, actor: AuthenticatedActor) -> None: ...


class AssistantAgentRuntime:
    """Narrow adapter injected into the Assistant application service."""

    def __init__(self, executor: AgentJobExecutor) -> None:
        self._executor = executor

    async def execute_job(self, *, job: AssistantJob, actor: AuthenticatedActor) -> None:
        await self._executor.execute_job(job=job, actor=actor)


class PostgreSQLExecutionRecorder(ExecutionRecorderPort):
    """Persist exact step identities using one short transaction per boundary."""

    def __init__(
        self,
        *,
        transaction_factory: AssistantTransactionFactory,
        registry: AgentRegistry,
        job: AssistantJob,
        block_projector: Any = None,
        actor: AuthenticatedActor | None = None,
    ) -> None:
        self._transactions = transaction_factory
        self._registry = registry
        self._job = job
        self._block_projector = block_projector
        self._actor = actor

    async def ensure_orchestrator_run(self) -> UUID:
        run_id = uuid5(NAMESPACE_URL, f"orchestrator:{self._job.turn_id}")
        async with self._transactions(self._job.organization_id) as transaction:
            existing = await transaction.repository.get_agent_run(
                organization_id=self._job.organization_id, run_id=run_id
            )
            if existing is None:
                registered = self._registry.resolve(AgentId.ORCHESTRATOR, "1.0.0", 4)
                run = AgentRun.create(
                    id=run_id,
                    organization_id=self._job.organization_id,
                    orchestration_run_id=self._job.orchestration_run_id,
                    agent_id=AgentId.ORCHESTRATOR.value,
                    agent_version=registered.manifest.agent.version,
                    manifest_fingerprint=registered.fingerprint,
                    capability="orchestration.delegate",
                    typed_input={"turn_id": str(self._job.turn_id)},
                    budget=registered.manifest.runtime.model_dump(mode="json"),
                ).mark_running()
                await transaction.repository.append_agent_run(run=run)
            elif existing.status in {
                DomainAgentRunStatus.AWAITING_INPUT,
                DomainAgentRunStatus.AWAITING_HUMAN,
            }:
                await transaction.repository.resume_agent_run(run=existing.resume())
            await transaction.commit()
        return run_id

    async def finish_orchestrator_run(self, run_id: UUID, output: OrchestratorOutput) -> None:
        async with self._transactions(self._job.organization_id) as transaction:
            run = await transaction.repository.get_agent_run(
                organization_id=self._job.organization_id, run_id=run_id
            )
            if run is None:
                raise RuntimeError("ORCHESTRATOR_AGENT_RUN_NOT_FOUND")
            if not run.status.is_terminal:
                if output.status.value == "FAILED":
                    updated = run.mark_failed("ORCHESTRATOR_MANUAL_FALLBACK")
                elif output.status.value in {"AWAITING_INPUT", "AWAITING_HUMAN"}:
                    updated = run.mark_awaiting(
                        status=DomainAgentRunStatus(output.status.value),
                        typed_output=output.model_dump(mode="json"),
                        stop_reason=output.stop_reason,
                    )
                else:
                    updated = run.mark_completed(
                        typed_output=output.model_dump(mode="json"),
                        stop_reason=output.stop_reason,
                    )
                await transaction.repository.finish_agent_run(run=updated)
            await transaction.commit()

    async def load_checkpoint(self, orchestration_run_id: UUID) -> ExecutionCheckpoint | None:
        async with self._transactions(self._job.organization_id) as transaction:
            value = await transaction.repository.load_orchestration_checkpoint(
                organization_id=self._job.organization_id,
                orchestration_run_id=orchestration_run_id,
            )
            await transaction.commit()
        if not value:
            return None
        return ExecutionCheckpoint.model_validate(value)

    async def start_agent_run(self, handoff: AgentHandoff) -> RecordedAgentRun:
        if (
            handoff.orchestration_run_id != self._job.orchestration_run_id
            or handoff.actor.organization_id != self._job.organization_id
        ):
            raise RuntimeError("AGENT_HANDOFF_SCOPE_MISMATCH")
        handoff_id = uuid5(NAMESPACE_URL, f"handoff:{handoff.idempotency_key}")
        run_id = uuid5(NAMESPACE_URL, f"agent-run:{handoff.idempotency_key}")
        async with self._transactions(self._job.organization_id) as transaction:
            existing = await transaction.repository.get_agent_run(
                organization_id=self._job.organization_id, run_id=run_id
            )
            if existing is not None:
                if (
                    existing.agent_id == "daily_update"
                    and existing.status is DomainAgentRunStatus.AWAITING_HUMAN
                ):
                    if existing.typed_output is None:
                        raise RuntimeError("DAILY_UPDATE_REPLAY_RESULT_MISSING")
                    await transaction.commit()
                    return RecordedAgentRun(
                        id=existing.id,
                        status=AgentRunStatus.AWAITING_HUMAN,
                        replayed_result=AgentResult.model_validate(existing.typed_output),
                    )
                if existing.status in {
                    DomainAgentRunStatus.AWAITING_INPUT,
                    DomainAgentRunStatus.AWAITING_HUMAN,
                }:
                    existing = existing.resume()
                    await transaction.repository.resume_agent_run(run=existing)
                await transaction.commit()
                replayed = (
                    AgentResult.model_validate(existing.typed_output)
                    if existing.status is DomainAgentRunStatus.COMPLETED
                    and existing.typed_output is not None
                    else None
                )
                return RecordedAgentRun(
                    id=existing.id,
                    status=AgentRunStatus(existing.status.value),
                    replayed_result=replayed,
                )
            registered = self._registry.resolve(
                handoff.target_agent_id, handoff.target_agent_version, 4
            )
            await transaction.repository.append_handoff(
                handoff=AgentHandoffRecord(
                    id=handoff_id,
                    organization_id=self._job.organization_id,
                    orchestration_run_id=handoff.orchestration_run_id,
                    parent_agent_run_id=handoff.parent_agent_run_id,
                    target_agent_id=handoff.target_agent_id.value,
                    target_agent_version=handoff.target_agent_version,
                    capability=handoff.capability,
                    objective=handoff.objective,
                    typed_input=cast(dict[str, object], handoff.typed_input),
                    context_references=tuple(
                        reference.model_dump(mode="json")
                        for reference in handoff.context_references
                    ),
                    budget=handoff.budget.model_dump(mode="json"),
                    step_id=handoff.step_id,
                    idempotency_key=handoff.idempotency_key,
                    dedupe_key=handoff.idempotency_key,
                )
            )
            run = AgentRun.create(
                id=run_id,
                organization_id=self._job.organization_id,
                orchestration_run_id=handoff.orchestration_run_id,
                parent_agent_run_id=handoff.parent_agent_run_id,
                inbound_handoff_id=handoff_id,
                agent_id=handoff.target_agent_id.value,
                agent_version=handoff.target_agent_version,
                manifest_fingerprint=registered.fingerprint,
                capability=handoff.capability,
                typed_input=cast(dict[str, object], handoff.typed_input),
                budget=handoff.budget.model_dump(mode="json"),
            ).mark_running()
            await transaction.repository.append_agent_run(run=run)
            await transaction.commit()
        return RecordedAgentRun(id=run.id, status=AgentRunStatus.RUNNING)

    async def finish_agent_run(self, run_id: UUID, result: AgentResult) -> None:
        async with self._transactions(self._job.organization_id) as transaction:
            run = await transaction.repository.get_agent_run(
                organization_id=self._job.organization_id, run_id=run_id
            )
            if run is None:
                raise RuntimeError("AGENT_RUN_NOT_FOUND")
            if result.status is AgentRunStatus.COMPLETED:
                updated = run.mark_completed(
                    typed_output=result.model_dump(mode="json"),
                    stop_reason=result.stop_reason,
                    usage={
                        "iterations": result.iterations_used,
                        "tool_calls": result.tool_calls_used,
                        "model_attempts": result.model_attempts_used,
                    },
                )
            elif result.status in {
                AgentRunStatus.AWAITING_INPUT,
                AgentRunStatus.AWAITING_HUMAN,
            }:
                updated = run.mark_awaiting(
                    status=DomainAgentRunStatus(result.status.value),
                    typed_output=result.model_dump(mode="json"),
                    stop_reason=result.stop_reason,
                    usage={
                        "iterations": result.iterations_used,
                        "tool_calls": result.tool_calls_used,
                        "model_attempts": result.model_attempts_used,
                    },
                )
            else:
                updated = run.mark_failed(result.safe_error_code or "AGENT_EXECUTION_FAILED")
            await transaction.repository.finish_agent_run(run=updated)
            await transaction.commit()

    async def save_checkpoint(self, checkpoint: ExecutionCheckpoint) -> None:
        async with self._transactions(self._job.organization_id) as transaction:
            await transaction.repository.save_checkpoint(
                checkpoint=AgentCheckpoint(
                    id=uuid5(
                        NAMESPACE_URL,
                        f"checkpoint:{checkpoint.orchestration_run_id}:{checkpoint.sequence}",
                    ),
                    organization_id=self._job.organization_id,
                    orchestration_run_id=checkpoint.orchestration_run_id,
                    agent_run_id=uuid5(NAMESPACE_URL, f"orchestrator:{self._job.turn_id}"),
                    sequence=checkpoint.sequence,
                    node=checkpoint.node,
                    typed_state=checkpoint.model_dump(mode="json"),
                    checkpoint_version="1.0.0",
                )
            )
            await transaction.repository.save_orchestration_checkpoint(
                organization_id=self._job.organization_id,
                orchestration_run_id=checkpoint.orchestration_run_id,
                checkpoint=checkpoint.model_dump(mode="json"),
                execution_plan=checkpoint.plan.model_dump(mode="json"),
            )
            await transaction.commit()

    async def append_public_blocks(
        self,
        conversation_id: UUID,
        turn_id: UUID,
        blocks: tuple[ResponseBlock, ...],
        dedupe_key: str,
    ) -> None:
        if conversation_id != self._job.conversation_id or turn_id != self._job.turn_id:
            raise RuntimeError("ASSISTANT_PUBLIC_BLOCK_SCOPE_MISMATCH")
        projected = tuple(block.model_dump(mode="json") for block in blocks)
        if self._block_projector is not None and self._actor is not None:
            projected = tuple(await self._block_projector.project(self._actor, projected))
        async with self._transactions(self._job.organization_id) as transaction:
            await transaction.repository.append_assistant_blocks(
                job=self._job,
                blocks=projected,
                dedupe_key=dedupe_key,
            )
            await transaction.commit()


class AssistantTurnExecutor:
    """Build one durable Orchestrator execution without an enclosing transaction."""

    def __init__(
        self,
        *,
        transaction_factory: AssistantTransactionFactory,
        registry: AgentRegistry,
        engine_factory: Callable[[ExecutionRecorderPort], AgentExecutionEngine],
        assignment_context_resolver: AssignmentContextResolverPort | None = None,
        daily_update_context_resolver: DailyUpdateContextResolver | None = None,
        block_projector: Any = None,
    ) -> None:
        self._block_projector = block_projector
        self._transactions = transaction_factory
        self._registry = registry
        self._engine_factory = engine_factory
        self._assignment_context_resolver = assignment_context_resolver
        self._daily_update_context_resolver = daily_update_context_resolver

    async def execute_job(self, *, job: AssistantJob, actor: AuthenticatedActor) -> None:
        async with self._transactions(actor) as transaction:
            run = await transaction.repository.begin_orchestration(job=job)
            snapshot = await transaction.repository.get_conversation_snapshot(
                actor=actor, conversation_id=job.conversation_id
            )
            await transaction.commit()
        if run.status in {
            OrchestrationRunStatus.COMPLETED,
            OrchestrationRunStatus.AWAITING_INPUT,
            OrchestrationRunStatus.AWAITING_HUMAN,
        }:
            return
        if snapshot is None:
            raise RuntimeError("ASSISTANT_CONVERSATION_NOT_FOUND")
        turn = next((item for item in snapshot.turns if item.id == job.turn_id), None)
        if turn is None or run.turn_id != turn.id:
            raise RuntimeError("ASSISTANT_EXECUTION_CONTEXT_INVALID")
        excerpts: list[ConversationExcerpt] = []
        active_planning = resolve_ambient_planning_context(snapshot.messages)
        pending_followup = None
        raw_pending = run.checkpoint.get("pending_followup")
        if isinstance(raw_pending, dict):
            try:
                pending_followup = PendingFollowup.model_validate(raw_pending)
            except ValueError:
                pending_followup = None
        active_team = resolve_ambient_team_context(
            snapshot.messages,
            pending_followup=pending_followup,
        )
        for message in snapshot.messages[-12:]:
            text = "\n".join(
                str(block.get("text", ""))
                for block in message.content_blocks
                if block.get("kind") == "text"
            ).strip()
            role = message.role.value
            if text and role == "USER":
                excerpts.append(ConversationExcerpt(role="USER", text=text))
            elif text and role == "ASSISTANT":
                excerpts.append(ConversationExcerpt(role="ASSISTANT", text=text))
            if message.id == turn.user_message_id and role == "USER":
                for block in message.content_blocks:
                    action = (
                        block.get("action")
                        if block.get("kind") == "accepted_card_action"
                        else block
                    )
                    if not isinstance(action, dict):
                        continue
                    trusted_action = cast(dict[str, object], action)
                    action_kind = trusted_action.get("kind")
                    requested_operation: Literal["RESUME_INPUT", "REVISE"]
                    if action_kind == "PLANNING_INPUT":
                        requested_operation = "RESUME_INPUT"
                    elif action_kind == "PLANNING_REVISE":
                        requested_operation = "REVISE"
                    else:
                        if action_kind != "TEAM_REVISE" or active_team is None:
                            continue
                        try:
                            recommendation_id = UUID(str(trusted_action["recommendation_id"]))
                            raw_version = trusted_action["recommendation_version"]
                            if not isinstance(raw_version, int):
                                continue
                            recommendation_version = raw_version
                        except (KeyError, TypeError, ValueError):
                            continue
                        if (
                            active_team.recommendation_id != recommendation_id
                            or active_team.recommendation_version != recommendation_version
                        ):
                            continue
                        active_team = active_team.model_copy(
                            update={"requested_operation": "REVISE_TEAM"}
                        )
                        break
                    try:
                        workflow_run_id = UUID(str(trusted_action["workflow_run_id"]))
                        proposal_id = (
                            UUID(str(trusted_action["proposal_id"]))
                            if trusted_action.get("proposal_id") is not None
                            else None
                        )
                    except (KeyError, ValueError):
                        continue
                    expected_version = trusted_action.get("expected_version")
                    if expected_version is not None and not isinstance(expected_version, int):
                        continue
                    proposal_version = expected_version
                    active_planning = ActivePlanningContext(
                        workflow_run_id=workflow_run_id,
                        workflow_status=(
                            "WAITING_FOR_DECISION"
                            if requested_operation == "REVISE"
                            else "NEEDS_INPUT"
                        ),
                        proposal_id=proposal_id,
                        proposal_version=proposal_version,
                        proposal_status=(
                            "READY_FOR_DECISION" if requested_operation == "REVISE" else None
                        ),
                        requested_operation=requested_operation,
                    )
                    break
        exact_assignment = None
        assignment_resolution_issue = None
        if self._assignment_context_resolver is not None:
            assignment_resolution = await self._assignment_context_resolver.resolve_assignment(
                actor=actor, message=turn.objective
            )
            exact_assignment = assignment_resolution.exact_context
            assignment_resolution_issue = assignment_resolution.issue
            if assignment_resolution.team_context is not None:
                active_team = assignment_resolution.team_context
        daily_update = None
        daily_issue = False
        if self._daily_update_context_resolver is not None:
            daily_update, daily_issue = await self._daily_update_context_resolver.resolve(
                actor=actor, message=turn.objective, locale=turn.locale
            )
        recorder = PostgreSQLExecutionRecorder(
            transaction_factory=self._transactions,
            registry=self._registry,
            job=job,
            block_projector=self._block_projector,
            actor=actor,
        )
        root_run_id = await recorder.ensure_orchestrator_run()
        await recorder.append_public_blocks(
            job.conversation_id,
            job.turn_id,
            (
                ActivityResponseBlock(
                    label_key="assistant.turn.running",
                    status="RUNNING",
                    agent_id=AgentId.ORCHESTRATOR,
                ),
            ),
            f"assistant:{job.turn_id}:running",
        )
        output = await self._engine_factory(recorder).execute(
            orchestration_run_id=job.orchestration_run_id,
            value=OrchestratorInput(
                orchestration_run_id=job.orchestration_run_id,
                conversation_id=job.conversation_id,
                turn_id=job.turn_id,
                message=turn.objective,
                locale=turn.locale,
                actor=ActorReference(
                    membership_id=actor.membership_id,
                    organization_id=actor.organization_id,
                ),
                active_context=ActiveConversationContext(
                    recent_messages=tuple(excerpts),
                    daily_update=daily_update,
                    daily_update_resolution_issue=daily_issue,
                    active_planning=active_planning,
                    active_team=active_team,
                    exact_assignment=exact_assignment,
                    assignment_resolution_issue=assignment_resolution_issue,
                ),
            ),
            recorder=recorder,
        )
        await recorder.finish_orchestrator_run(root_run_id, output)
        safe_error = "ORCHESTRATOR_MANUAL_FALLBACK" if output.status.value == "FAILED" else None
        async with self._transactions(actor) as transaction:
            await transaction.repository.finish_orchestration(
                job=job,
                status=output.status.value,
                stop_reason=output.stop_reason,
                safe_error_code=safe_error,
            )
            await transaction.commit()
