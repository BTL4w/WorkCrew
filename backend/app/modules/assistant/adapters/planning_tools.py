"""Proposal-only Assistant bridge to focused Planning application services."""

from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from app.modules.assistant.application.ports import AssistantTransactionFactory
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.planning_runs.application.proposal_service import ProposalService
from app.modules.planning_runs.application.run_service import PlanningRunService
from app.modules.planning_runs.domain.models import (
    IdempotencyKeyReusedError,
    PlanningRunDomainError,
    ResourceVersionMismatchError,
    WorkflowRunStatus,
)
from work_management_ai.agents.planning.contracts import (
    PlanningAgentInput,
    PlanningAgentOutput,
    PlanningOperation,
)
from work_management_ai.runtime.contracts import (
    ActorReference,
    ToolExecutionRequest,
    ToolExecutionResult,
)


class CurrentActorResolverPort(Protocol):
    async def resolve(
        self, *, organization_id: UUID, membership_id: UUID
    ) -> AuthenticatedActor | None: ...


class AssistantPlanningToolAdapter:
    """Re-resolve authority and bind model requests to durable Agent state."""

    def __init__(
        self,
        *,
        actor_resolver: CurrentActorResolverPort,
        assistant_transaction_factory: AssistantTransactionFactory,
        planning_run_service: PlanningRunService,
        proposal_service: ProposalService,
    ) -> None:
        self._actors = actor_resolver
        self._assistant_transactions = assistant_transaction_factory
        self._planning_runs = planning_run_service
        self._proposals = proposal_service

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        if request.tool_id != "planning.manage_run" or request.tool_version != "1.0.0":
            return self._reject("TOOL_IDENTITY_MISMATCH")
        try:
            value = PlanningAgentInput.model_validate(request.typed_input)
        except ValidationError:
            return self._reject("PLANNING_INPUT_INVALID")
        actor = await self._resolve(request.actor)
        if actor is None:
            return self._reject("ACTOR_CONTEXT_UNAVAILABLE")
        async with self._assistant_transactions(actor) as transaction:
            context = await transaction.repository.get_agent_run_turn_context(
                organization_id=actor.organization_id,
                run_id=request.agent_run_id,
            )
            await transaction.commit()
        if context is None:
            return self._reject("PLANNING_RUN_NOT_FOUND")
        agent_run, turn_id = context
        if agent_run.agent_id != "planning" or agent_run.organization_id != actor.organization_id:
            return self._reject("PLANNING_RUN_NOT_FOUND")
        try:
            if value.risk_context is not None:
                result = await self._proposals.request_risk_revision(
                    actor=actor,
                    risk_context=value.risk_context.model_dump(mode="json"),
                    instruction=value.manager_instruction or "",
                    locale=value.locale,
                    request_id=f"assistant-tool:{request.agent_run_id}:{request.call_id}",
                    idempotency_key=request.idempotency_key,
                )
                async with self._assistant_transactions(actor) as transaction:
                    await transaction.repository.link_agent_workflow_run(
                        organization_id=actor.organization_id,
                        agent_run_id=request.agent_run_id,
                        workflow_run_id=result.run.id,
                    )
                    await transaction.commit()
                return self._success(
                    value,
                    workflow_run_id=result.run.id,
                    workflow_status=result.run.status.value,
                    awaiting="MANAGER_DECISION",
                )
            if value.operation is PlanningOperation.CREATE:
                result = await self._planning_runs.create_planning_run(
                    actor=actor,
                    message=value.brief,
                    locale=value.locale,
                    request_id=f"assistant-tool:{request.agent_run_id}:{request.call_id}",
                    idempotency_key=f"assistant:{turn_id}:planning:create",
                )
                async with self._assistant_transactions(actor) as transaction:
                    await transaction.repository.link_agent_workflow_run(
                        organization_id=actor.organization_id,
                        agent_run_id=request.agent_run_id,
                        workflow_run_id=result.run.id,
                    )
                    await transaction.commit()
                return self._success(
                    value,
                    workflow_run_id=result.run.id,
                    workflow_status=result.run.status.value,
                )
            if value.operation is PlanningOperation.RESUME_INPUT:
                if (
                    value.workflow_run_id is None
                    or agent_run.workflow_run_id != value.workflow_run_id
                ):
                    return self._reject("PLANNING_RUN_NOT_FOUND")
                snapshot = await self._planning_runs.get_workflow_run_snapshot(
                    actor=actor,
                    run_id=value.workflow_run_id,
                )
                if (
                    snapshot.run.status is not WorkflowRunStatus.NEEDS_INPUT
                    or snapshot.checkpoint is None
                    or snapshot.checkpoint.node != "await_manager_input"
                ):
                    return self._reject("PLANNING_RUN_NOT_AWAITING_INPUT")
                result = await self._planning_runs.post_manager_message(
                    actor=actor,
                    run_id=value.workflow_run_id,
                    message=value.manager_instruction or "",
                    request_id=f"assistant-tool:{request.agent_run_id}:{request.call_id}",
                    idempotency_key=request.idempotency_key,
                )
                return self._success(
                    value,
                    workflow_run_id=result.run.id,
                    workflow_status=result.run.status.value,
                )
            if value.operation is PlanningOperation.REVISE:
                async with self._assistant_transactions(actor) as transaction:
                    accepted = await transaction.repository.get_accepted_planning_action(
                        organization_id=actor.organization_id,
                        turn_id=turn_id,
                    )
                    await transaction.commit()
                if (
                    accepted is None
                    or accepted.get("kind") != "PLANNING_REVISE"
                    or accepted.get("proposal_id") is None
                    or not isinstance(accepted.get("expected_version"), int)
                ):
                    return self._reject("PLANNING_INPUT_INVALID")
                try:
                    proposal_id = UUID(str(accepted["proposal_id"]))
                except ValueError:
                    return self._reject("PLANNING_INPUT_INVALID")
                expected_version = int(accepted["expected_version"])
                result = await self._proposals.request_ai_revision(
                    actor=actor,
                    proposal_id=proposal_id,
                    expected_version=expected_version,
                    instruction=value.manager_instruction or "",
                    request_id=f"assistant-tool:{request.agent_run_id}:{request.call_id}",
                    idempotency_key=request.idempotency_key,
                )
                async with self._assistant_transactions(actor) as transaction:
                    await transaction.repository.link_agent_workflow_run(
                        organization_id=actor.organization_id,
                        agent_run_id=request.agent_run_id,
                        workflow_run_id=result.workflow_run_id,
                    )
                    await transaction.commit()
                return self._success(
                    value,
                    workflow_run_id=result.workflow_run_id,
                    workflow_status="RUNNING",
                    proposal_id=result.proposal_id,
                    proposal_version=result.base_version,
                    awaiting="MANAGER_DECISION",
                )
            if value.workflow_run_id is None or agent_run.workflow_run_id != value.workflow_run_id:
                return self._reject("PLANNING_RUN_NOT_FOUND")
            snapshot = await self._planning_runs.get_workflow_run_snapshot(
                actor=actor,
                run_id=value.workflow_run_id,
            )
            if snapshot.proposal is None or snapshot.proposal.id != value.proposal_id:
                return self._reject("PLANNING_RUN_NOT_FOUND")
            return self._success(
                value,
                workflow_run_id=snapshot.run.id,
                workflow_status=snapshot.run.status.value,
                proposal_id=snapshot.proposal.id,
                proposal_version=snapshot.proposal.current_version_number,
                approval_id=snapshot.proposal.approval_id,
                awaiting=(
                    "MANAGER_DECISION"
                    if snapshot.run.status is WorkflowRunStatus.WAITING_FOR_DECISION
                    else "NONE"
                ),
            )
        except IdempotencyKeyReusedError:
            return self._reject("IDEMPOTENCY_KEY_REUSED")
        except ResourceVersionMismatchError:
            return self._reject("RESOURCE_VERSION_MISMATCH")
        except PlanningRunDomainError:
            return self._reject("PLANNING_RUN_NOT_FOUND")

    async def _resolve(self, reference: ActorReference) -> AuthenticatedActor | None:
        actor = await self._actors.resolve(
            organization_id=reference.organization_id,
            membership_id=reference.membership_id,
        )
        if actor is None or (
            actor.organization_id != reference.organization_id
            or actor.membership_id != reference.membership_id
        ):
            return None
        return actor

    @staticmethod
    def _success(
        value: PlanningAgentInput,
        *,
        workflow_run_id: UUID,
        workflow_status: str,
        proposal_id: UUID | None = None,
        proposal_version: int | None = None,
        approval_id: UUID | None = None,
        awaiting: str = "NONE",
    ) -> ToolExecutionResult:
        output = PlanningAgentOutput(
            operation=value.operation,
            workflow_run_id=workflow_run_id,
            workflow_status=workflow_status,
            proposal_id=proposal_id,
            proposal_version=proposal_version,
            approval_id=approval_id,
            awaiting=awaiting,  # type: ignore[arg-type]
            public_summary="Planning workflow updated.",
        )
        return ToolExecutionResult(status="SUCCEEDED", typed_output=output.model_dump(mode="json"))

    @staticmethod
    def _reject(code: str) -> ToolExecutionResult:
        return ToolExecutionResult(status="REJECTED", typed_output={}, safe_error_code=code)
