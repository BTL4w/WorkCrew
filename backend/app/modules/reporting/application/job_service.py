"""One bounded generation claim per worker tick; providers run outside transactions."""

import asyncio
from contextlib import suppress
from typing import Protocol
from uuid import UUID

from work_management_ai.agents.reporting.contracts import ReportingUsageScope

from ..domain.generation import GenerationJob
from .ports import GenerationTransactionFactory


class GenerationExecutor(Protocol):
    async def execute(self, job: GenerationJob, scope: ReportingUsageScope) -> bool: ...


class ReportJobService:
    def __init__(
        self, transactions: GenerationTransactionFactory, executor: GenerationExecutor | None = None
    ):
        self.transactions, self.executor = transactions, executor

    async def claim(self, *, worker_id: str, organization_id: UUID) -> GenerationJob | None:
        async with self.transactions(organization_id) as repo:
            return await repo.claim(worker_id)

    async def _heartbeat(self, scope: ReportingUsageScope) -> None:
        while True:
            await asyncio.sleep(10)
            async with self.transactions(scope.organization_id) as repo:
                await repo.heartbeat(scope)

    async def run_once(self, *, worker_id: str, organization_id: UUID) -> bool:
        if self.executor is None:
            raise ValueError("REPORTING_EXECUTOR_REQUIRED")
        async with self.transactions(organization_id) as repo:
            recovery = await repo.recoverable()
            if recovery is not None:
                # This branch cannot call a provider. Hold only this job's row lock
                # through recorder reconciliation so recovery workers cannot race.
                scope = ReportingUsageScope(
                    organization_id=organization_id,
                    membership_id=recovery.requester_membership_id,
                    generation_id=recovery.id,
                    fence=recovery.fence,
                    worker_id=recovery.lease_owner or worker_id,
                )
                try:
                    succeeded = await self.executor.execute(recovery, scope)
                except Exception:
                    succeeded = False
                await repo.reconcile(recovery, succeeded=succeeded)
                return True
        job = await self.claim(worker_id=worker_id, organization_id=organization_id)
        if job is None:
            return False
        scope = ReportingUsageScope(
            organization_id=organization_id,
            membership_id=job.requester_membership_id,
            generation_id=job.id,
            fence=job.fence,
            worker_id=worker_id,
        )
        heartbeat = asyncio.create_task(self._heartbeat(scope))
        try:
            succeeded = await self.executor.execute(job, scope)
            async with self.transactions(organization_id) as repo:
                await repo.complete(
                    scope, succeeded=succeeded, code=None if succeeded else "REPORTING_UNAVAILABLE"
                )
        except Exception:
            with suppress(Exception):
                async with self.transactions(organization_id) as repo:
                    await repo.complete(scope, succeeded=False, code="REPORTING_JOB_FAILED")
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await heartbeat
        return True
