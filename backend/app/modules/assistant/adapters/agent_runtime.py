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
from app.modules.assistant.adapters.execution_recorder import PostgreSQLExecutionRecorder
from app.modules.assistant.adapters.report_chat_usage import report_chat_job_scope
from app.modules.assistant.application.ports import AssistantTransactionFactory
from app.modules.assistant.domain.models import (
    AgentModelInvocation,
    AssistantJob,
    AssistantMessage,
    InvocationStatus,
    OrchestrationRunStatus,
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
    OrchestratorTriggerInput,
    PendingFollowup,
)
from work_management_ai.agents.orchestrator.harness import OrchestratorHarness
from work_management_ai.agents.planning.harness import PlanningAgentHarness
from work_management_ai.agents.reporting.harness import ReportingHarness
from work_management_ai.agents.reporting.usage import ReportingUsagePort
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
    ResolvedActorContext,
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
    ExecutionRecorderPort,
)
from work_management_ai.runtime.manifests import SkillManifest, ToolManifest, load_yaml_resource
from work_management_ai.runtime.policy_guard import PolicyGuard
from work_management_ai.runtime.skill_registry import SkillRegistry
from work_management_ai.runtime.tool_registry import ToolRegistry

_SKILL_RESOURCES = (
    ("work_management_ai.skills.draft_management_report", "skill.yaml"),
    ("work_management_ai.skills.summarize_verified_project_metrics", "skill.yaml"),
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
    ("work_management_ai.tools.reporting", "tool.yaml"),
    ("work_management_ai.tools.reporting", "propose.yaml"),
    ("work_management_ai.tools.reporting", "chat.yaml"),
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
    ("work_management_ai.agents.reporting", "agent.yaml"),
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
        "reporting_numeric@1",
        "reporting_grounding@1",
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
    def __init__(
        self,
        harness: AgentHarness,
        usage_store: UsageStore | None = None,
        reporting_usage_factory: Callable[[AgentHandoff], ReportingUsagePort] | None = None,
    ) -> None:
        self._harness = harness
        self._usage_store = usage_store
        self._reporting_usage_factory = reporting_usage_factory

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
            if self._reporting_usage_factory is not None and isinstance(
                self._harness, ReportingHarness
            ):
                harness = ReportingHarness(
                    model_gateway=self._harness.gateway,
                    tool_executor=self._harness.tools,
                    actor_resolver=self._harness.actors,
                    usage=self._reporting_usage_factory(handoff),
                )
                return await harness.run(handoff)
            return await self._harness.run(handoff)


class _ScopedOrchestrator:
    def __init__(self, harness: OrchestratorHarness) -> None:
        self._harness = harness

    async def run_turn(self, value: OrchestratorInput) -> OrchestratorOutput:
        run_id = uuid5(NAMESPACE_URL, f"orchestrator:{value.turn_id}")
        with agent_model_scope(value.actor.organization_id, run_id):
            return await self._harness.run_turn(value)

    async def run_trigger(self, value: OrchestratorTriggerInput) -> OrchestratorOutput:
        with agent_model_scope(
            value.actor.organization_id,
            uuid5(NAMESPACE_URL, f"orchestrator:{value.orchestration_run_id}"),
        ):
            return await self._harness.run_trigger(value)


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
    reporting_tool_executor: ToolExecutorPort | None = None,
    reporting_usage: ReportingUsagePort | None = None,
    reporting_usage_factory: Callable[[AgentHandoff], ReportingUsagePort] | None = None,
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

    if reporting_tool_executor is not None:
        harnesses = {
            **harnesses,
            AgentId.REPORTING: _ScopedAgentHarness(
                ReportingHarness(
                    model_gateway=recording_gateway,
                    tool_executor=reporting_tool_executor,
                    actor_resolver=agent_actor_resolver,
                    usage=reporting_usage,
                ),
                reporting_usage_factory=reporting_usage_factory,
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
        with report_chat_job_scope(job):
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
            await recorder.check_claim(transaction.repository)
            await transaction.repository.finish_orchestration(
                job=job,
                status=output.status.value,
                stop_reason=output.stop_reason,
                safe_error_code=safe_error,
            )
            await transaction.commit()
