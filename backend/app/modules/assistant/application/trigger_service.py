"""Authorized idempotent non-chat run creation, without specialist dispatch."""

from contextlib import AbstractAsyncContextManager
from typing import Protocol
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from work_management_ai.runtime.agent_registry import AgentRegistry
from work_management_ai.runtime.contracts import AgentId

from ..domain.models import OrchestrationRun
from ..domain.triggers import ExecutionTrigger, TriggerError


class TriggerRepository(Protocol):
    async def authenticate(self, trigger: ExecutionTrigger) -> None: ...
    async def ensure(
        self, trigger: ExecutionTrigger, version: str, fingerprint: str, budget: dict[str, object]
    ) -> OrchestrationRun: ...
    async def reject(self, trigger: ExecutionTrigger, reason: str) -> None: ...
    async def start(self, run_id: UUID) -> None: ...
    async def finish(self, run_id: UUID, *, succeeded: bool, usage: dict[str, int]) -> None: ...


class TriggerTransactionFactory(Protocol):
    def __call__(
        self, actor: AuthenticatedActor
    ) -> AbstractAsyncContextManager[TriggerRepository]: ...


class TriggerService:
    def __init__(self, transactions: TriggerTransactionFactory, registry: AgentRegistry):
        self.transactions, self.registry = transactions, registry

    async def ensure(
        self, *, actor: AuthenticatedActor, trigger: ExecutionTrigger
    ) -> OrchestrationRun:
        hub = self.registry.resolve(AgentId.ORCHESTRATOR, "1.0.0", 5)
        try:
            async with self.transactions(actor) as repo:
                await repo.authenticate(trigger)
                return await repo.ensure(
                    trigger,
                    hub.manifest.agent.version,
                    hub.fingerprint,
                    hub.manifest.runtime.model_dump(mode="json"),
                )
        except TriggerError as exc:
            async with self.transactions(actor) as repo:
                await repo.reject(trigger, exc.code)
            raise

    async def start(self, *, actor: AuthenticatedActor, run_id: UUID) -> None:
        async with self.transactions(actor) as repo:
            await repo.start(run_id)

    async def finish(
        self, *, actor: AuthenticatedActor, run_id: UUID, succeeded: bool, usage: dict[str, int]
    ) -> None:
        async with self.transactions(actor) as repo:
            await repo.finish(run_id, succeeded=succeeded, usage=usage)
