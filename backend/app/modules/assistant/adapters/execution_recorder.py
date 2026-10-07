"""Reusable durable step/checkpoint recording with separate chat and typed sinks."""

from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from app.modules.assistant.application.ports import AssistantRepository, AssistantTransactionFactory
from app.modules.assistant.domain.models import (
    AgentCheckpoint,
    AgentHandoffRecord,
    AgentRun,
    AssistantJob,
)
from app.modules.assistant.domain.models import AgentRunStatus as DomainAgentRunStatus
from app.modules.identity.domain.auth import AuthenticatedActor
from work_management_ai.agents.orchestrator.contracts import OrchestratorOutput
from work_management_ai.runtime.agent_registry import AgentRegistry
from work_management_ai.runtime.contracts import (
    AgentHandoff,
    AgentId,
    AgentResult,
    AgentRunStatus,
    ResponseBlock,
)
from work_management_ai.runtime.execution_engine import (
    ExecutionCheckpoint,
    ExecutionRecorderPort,
    RecordedAgentRun,
)
from work_management_ai.runtime.triggers import ChatTurnTrigger, ExecutionScope


class PostgreSQLExecutionRecorder(ExecutionRecorderPort):
    """Persist exact step identities using one short transaction per boundary."""

    def __init__(
        self,
        *,
        transaction_factory: AssistantTransactionFactory,
        registry: AgentRegistry,
        job: AssistantJob | None = None,
        scope: ExecutionScope | None = None,
        block_projector: Any = None,
        actor: AuthenticatedActor | None = None,
    ) -> None:
        self._transactions = transaction_factory
        self._registry = registry
        self._job = job
        self._report_chat_active = False
        if scope is None:
            if job is None:
                raise ValueError("EXECUTION_SCOPE_REQUIRED")
            scope = ExecutionScope(
                organization_id=job.organization_id,
                actor_membership_id=job.requester_membership_id,
                orchestration_run_id=job.orchestration_run_id,
                trigger=ChatTurnTrigger(turn_id=job.turn_id),
            )
        elif job is not None:
            raise ValueError("EXECUTION_SCOPE_EXCLUSIVE")
        if not isinstance(scope.trigger, ChatTurnTrigger) and (
            actor is None
            or actor.organization_id != scope.organization_id
            or actor.membership_id != scope.actor_membership_id
        ):
            raise ValueError("EXECUTION_ACTOR_SCOPE_MISMATCH")
        self._scope = scope
        self._identity = scope.identity
        self._context: AuthenticatedActor | UUID = (
            actor
            if actor is not None and not isinstance(scope.trigger, ChatTurnTrigger)
            else scope.organization_id
        )
        self._phase = 5
        self._block_projector = block_projector
        self._actor = actor

    async def check_claim(self, repository: AssistantRepository) -> None:
        await repository.load_orchestration_checkpoint(
            organization_id=self._scope.organization_id,
            orchestration_run_id=self._scope.orchestration_run_id,
        )
        if self._report_chat_active and self._job is not None:
            await repository.assert_job_claim(job=self._job)

    async def ensure_orchestrator_run(self) -> UUID:
        run_id = uuid5(NAMESPACE_URL, f"orchestrator:{self._identity}")
        async with self._transactions(self._context) as transaction:
            await self.check_claim(transaction.repository)
            existing = await transaction.repository.get_agent_run(
                organization_id=self._scope.organization_id, run_id=run_id
            )
            if existing is None:
                registered = self._registry.resolve(AgentId.ORCHESTRATOR, "1.0.0", self._phase)
                run = AgentRun.create(
                    id=run_id,
                    organization_id=self._scope.organization_id,
                    orchestration_run_id=self._scope.orchestration_run_id,
                    agent_id=AgentId.ORCHESTRATOR.value,
                    agent_version=registered.manifest.agent.version,
                    manifest_fingerprint=registered.fingerprint,
                    capability="orchestration.delegate",
                    typed_input=self._scope.trigger.model_dump(mode="json"),
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
        if run_id != uuid5(NAMESPACE_URL, f"orchestrator:{self._identity}"):
            raise RuntimeError("ORCHESTRATOR_AGENT_RUN_SCOPE_MISMATCH")
        async with self._transactions(self._context) as transaction:
            await self.check_claim(transaction.repository)
            run = await transaction.repository.get_agent_run(
                organization_id=self._scope.organization_id, run_id=run_id
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
        if orchestration_run_id != self._scope.orchestration_run_id:
            raise RuntimeError("EXECUTION_CHECKPOINT_SCOPE_MISMATCH")
        async with self._transactions(self._context) as transaction:
            await self.check_claim(transaction.repository)
            value = await transaction.repository.load_orchestration_checkpoint(
                organization_id=self._scope.organization_id,
                orchestration_run_id=orchestration_run_id,
            )
            await transaction.commit()
        if not value:
            return None
        return ExecutionCheckpoint.model_validate(value)

    async def start_agent_run(self, handoff: AgentHandoff) -> RecordedAgentRun:
        if handoff.capability in {"reporting.prepare_report", "reporting.explain_snapshot"}:
            self._report_chat_active = True
        if (
            handoff.orchestration_run_id != self._scope.orchestration_run_id
            or handoff.actor.organization_id != self._scope.organization_id
            or handoff.actor.membership_id != self._scope.actor_membership_id
            or handoff.parent_agent_run_id != uuid5(NAMESPACE_URL, f"orchestrator:{self._identity}")
        ):
            raise RuntimeError("AGENT_HANDOFF_SCOPE_MISMATCH")
        handoff_id = uuid5(NAMESPACE_URL, f"handoff:{handoff.idempotency_key}")
        run_id = uuid5(NAMESPACE_URL, f"agent-run:{handoff.idempotency_key}")
        async with self._transactions(self._context) as transaction:
            await self.check_claim(transaction.repository)
            existing = await transaction.repository.get_agent_run(
                organization_id=self._scope.organization_id, run_id=run_id
            )
            if existing is not None:
                if (
                    existing.agent_id in {"daily_update", "reporting"}
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
                handoff.target_agent_id, handoff.target_agent_version, self._phase
            )
            await transaction.repository.append_handoff(
                handoff=AgentHandoffRecord(
                    id=handoff_id,
                    organization_id=self._scope.organization_id,
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
                organization_id=self._scope.organization_id,
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
        if result.agent_id is AgentId.REPORTING and self._job is not None:
            self._report_chat_active = True
        async with self._transactions(self._context) as transaction:
            await self.check_claim(transaction.repository)
            run = await transaction.repository.get_agent_run(
                organization_id=self._scope.organization_id, run_id=run_id
            )
            if (
                run is None
                or run.orchestration_run_id != self._scope.orchestration_run_id
                or run.agent_id != result.agent_id.value
                or run.agent_version != result.agent_version
            ):
                raise RuntimeError("AGENT_RUN_NOT_FOUND")
            if (
                run.agent_id == "reporting"
                and run.status is DomainAgentRunStatus.AWAITING_HUMAN
                and result.status is AgentRunStatus.AWAITING_HUMAN
            ):
                if (
                    run.typed_output is None
                    or run.typed_output.get("typed_output") != result.typed_output
                ):
                    raise RuntimeError("REPORTING_RESULT_REPLAY_MISMATCH")
                await transaction.commit()
                return
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
        if checkpoint.orchestration_run_id != self._scope.orchestration_run_id:
            raise RuntimeError("EXECUTION_CHECKPOINT_SCOPE_MISMATCH")
        async with self._transactions(self._context) as transaction:
            await self.check_claim(transaction.repository)
            await transaction.repository.save_checkpoint(
                checkpoint=AgentCheckpoint(
                    id=uuid5(
                        NAMESPACE_URL,
                        f"checkpoint:{checkpoint.orchestration_run_id}:{checkpoint.sequence}",
                    ),
                    organization_id=self._scope.organization_id,
                    orchestration_run_id=checkpoint.orchestration_run_id,
                    agent_run_id=uuid5(NAMESPACE_URL, f"orchestrator:{self._identity}"),
                    sequence=checkpoint.sequence,
                    node=checkpoint.node,
                    typed_state=checkpoint.model_dump(mode="json"),
                    checkpoint_version="1.0.0",
                )
            )
            await transaction.repository.save_orchestration_checkpoint(
                organization_id=self._scope.organization_id,
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
        if (
            self._job is None
            or conversation_id != self._job.conversation_id
            or turn_id != self._identity
        ):
            raise RuntimeError("ASSISTANT_PUBLIC_BLOCK_SCOPE_MISMATCH")
        projected = tuple(block.model_dump(mode="json") for block in blocks)
        if self._block_projector is not None and self._actor is not None:
            projected = tuple(await self._block_projector.project(self._actor, projected))
        async with self._transactions(self._context) as transaction:
            await self.check_claim(transaction.repository)
            await transaction.repository.append_assistant_blocks(
                job=self._job,
                blocks=projected,
                dedupe_key=dedupe_key,
            )
            await transaction.commit()


class PostgreSQLTriggerRecorder(PostgreSQLExecutionRecorder):
    """Typed non-chat result sink. A report/scheduler run can never append transcript facts."""

    def __init__(
        self,
        *,
        transaction_factory: AssistantTransactionFactory,
        registry: AgentRegistry,
        scope: ExecutionScope,
        actor: AuthenticatedActor,
    ):
        if isinstance(scope.trigger, ChatTurnTrigger):
            raise ValueError("NON_CHAT_TRIGGER_REQUIRED")
        super().__init__(
            transaction_factory=transaction_factory, registry=registry, scope=scope, actor=actor
        )
