"""Bind the isolated Harness to canonical reservations, leases and checkpoint CAS."""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from app.modules.assistant.adapters.retrieval_tools import RetrievalToolExecutor
from app.modules.assistant.adapters.work_tools import (
    CurrentActorResolverPort,
    RecordingToolExecutor,
)
from app.modules.assistant.application.ports import AssistantTransactionFactory
from app.modules.assistant.domain.models import AssistantJob
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.retrieval.application.budget_service import RetrievalBudgetService
from app.modules.retrieval.application.service import RetrievalService
from app.modules.retrieval.domain.models import RetrievalPolicyError, RetrievalReservation
from app.modules.retrieval.domain.state import RetrievalLease
from work_management_ai.agents.retrieve.budget import ModelReservation, RetrievalCheckpoint
from work_management_ai.model_gateway.contracts import ModelUsage
from work_management_ai.runtime.contracts import (
    ActorReference,
    AgentHandoff,
    AgentResult,
    ToolExecutionRequest,
    ToolExecutionResult,
)
from work_management_ai.runtime.tool_registry import ToolRegistry


class RetrievalUsageAdapter:
    def __init__(
        self,
        *,
        budgets: RetrievalBudgetService,
        actor: AuthenticatedActor,
        run_id: UUID,
        lease: RetrievalLease,
    ):
        self.budgets, self.actor, self.run_id, self.lease = budgets, actor, run_id, lease
        self.receipts: dict[str, RetrievalReservation] = {}

    async def remaining_seconds(self, *, retrieval: bool = False) -> float:
        return await self.budgets.remaining_seconds(
            self.actor, self.run_id, self.lease, retrieval=retrieval
        )

    async def load_checkpoint(self) -> RetrievalCheckpoint | None:
        value = await self.budgets.load_checkpoint(self.actor, self.run_id, self.lease)
        return RetrievalCheckpoint.model_validate(value) if value else None

    async def save_checkpoint(self, checkpoint: RetrievalCheckpoint) -> None:
        await self.budgets.save_checkpoint(
            self.actor, self.run_id, self.lease, checkpoint.model_dump(mode="json")
        )

    async def reserve_node(self, operation_key: str) -> None:
        receipt = await self.budgets.reserve_attempt(
            self.actor, self.run_id, operation_key, 1, "NODE", lease=self.lease
        )
        await self.budgets.record_attempt_result(self.actor, receipt, lease=self.lease, metadata={})

    async def reserve_model_attempt(
        self, *, attempt_no: int, input_tokens: int, output_tokens: int
    ) -> ModelReservation:
        receipt = await self.budgets.reserve_attempt(
            self.actor,
            self.run_id,
            "synthesis",
            attempt_no,
            "MODEL",
            lease=self.lease,
            token_bounds=(input_tokens, output_tokens),
        )
        self.receipts[str(receipt.id)] = receipt
        return ModelReservation(reservation_id=str(receipt.id), attempt_no=attempt_no)

    async def record_model_usage(
        self,
        reservation: ModelReservation,
        usage: ModelUsage | None,
        *,
        safe_code: str | None = None,
    ) -> None:
        receipt = self.receipts[reservation.reservation_id]
        metadata: dict[str, Any] = dict(
            safe_code=safe_code,
            retryable=safe_code in {"MODEL_TIMEOUT", "MODEL_RATE_LIMITED"},
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
        )
        try:
            await self.budgets.record_attempt_result(
                self.actor, receipt, lease=self.lease, metadata=metadata
            )
        except RetrievalPolicyError as error:
            # Canonical storage has already fenced this overrun as UNKNOWN. The
            # Harness independently checks its bounds, records the observed usage
            # and falls back to evidence. Other policy failures remain refusals.
            if str(error) != "RETRIEVAL_OBSERVED_USAGE_EXCEEDED":
                raise


_RETRIEVAL_JOB: ContextVar[AssistantJob | None] = ContextVar("retrieval_job", default=None)


@contextmanager
def retrieval_job_scope(job: AssistantJob) -> Generator[None]:
    token = _RETRIEVAL_JOB.set(job)
    try:
        yield
    finally:
        _RETRIEVAL_JOB.reset(token)


class RetrievalRuntime:
    def __init__(
        self,
        *,
        budgets: RetrievalBudgetService,
        service: RetrievalService,
        actors: CurrentActorResolverPort,
        tools: ToolRegistry,
        transactions: AssistantTransactionFactory,
    ):
        self.budgets, self.service, self.actors, self.tools, self.transactions = (
            budgets,
            service,
            actors,
            tools,
            transactions,
        )

    def _job(self) -> AssistantJob:
        job = _RETRIEVAL_JOB.get()
        if job is None or job.locked_by is None:
            raise ValueError("RETRIEVAL_JOB_CLAIM_REQUIRED")
        return job

    def _backend(self) -> RetrievalToolExecutor:
        job = self._job()
        return RetrievalToolExecutor(
            budgets=self.budgets,
            service=self.service,
            actor_resolver=self.actors,
            tool_registry=self.tools,
            lease=RetrievalLease(job.id, job.locked_by or "", job.attempt_count),
        )

    async def usage(self, handoff: AgentHandoff) -> RetrievalUsageAdapter:
        job = self._job()
        actor = await self.actors.resolve(
            organization_id=handoff.actor.organization_id, membership_id=handoff.actor.membership_id
        )
        if (
            actor is None
            or actor.organization_id != job.organization_id
            or actor.membership_id != job.requester_membership_id
            or handoff.orchestration_run_id != job.orchestration_run_id
        ):
            raise ValueError("RETRIEVAL_ACTOR_UNAVAILABLE")
        return RetrievalUsageAdapter(
            budgets=self.budgets,
            actor=actor,
            run_id=uuid5(NAMESPACE_URL, f"agent-run:{handoff.idempotency_key}"),
            lease=RetrievalLease(job.id, job.locked_by or "", job.attempt_count),
        )

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        backend = self._backend()
        return await RecordingToolExecutor(
            transaction_factory=self.transactions,
            tool_registry=self.tools,
            backend=backend,
            replay_validator=backend,
            job_claim=self._job(),
        ).execute(request)

    async def revalidate_agent_result(
        self, run_id: UUID, reference: ActorReference, result: AgentResult
    ) -> AgentResult:
        return await self._backend().revalidate_agent_result(run_id, reference, result)

    async def validate_public_intent(
        self, intent: dict[str, Any], message: str, reference: ActorReference
    ) -> None:
        actor = await self.actors.resolve(
            organization_id=reference.organization_id, membership_id=reference.membership_id
        )
        if (
            actor is None
            or actor.organization_id != reference.organization_id
            or actor.membership_id != reference.membership_id
        ):
            raise ValueError("RETRIEVAL_ACTOR_UNAVAILABLE")
        await self.budgets.validate_public_intent(actor, intent, message)
