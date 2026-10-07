"""Explicit Admin start and bounded evaluation execution with fresh identity and fencing."""

import asyncio
from contextlib import suppress
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.reporting.domain.reports import ReportError

from ..domain.evaluation_runs import EvaluationRequest, EvaluationRun
from .ports import EvaluationPolicyPort, EvaluationProviderPort, EvaluationTransactionsPort


class EvaluationService:
    def __init__(
        self,
        transactions: EvaluationTransactionsPort,
        *,
        policy: EvaluationPolicyPort,
        provider: EvaluationProviderPort | None = None,
    ):
        self.transactions = transactions
        self.policy = policy
        self.provider = provider

    async def audit_rejection(
        self, *, actor: AuthenticatedActor, key: str | None, reason_code: str
    ) -> None:
        async with self.transactions(actor) as repo:
            await repo.evidence("run.started", key or "", None, reason_code)

    async def start(
        self, *, actor: AuthenticatedActor, request: EvaluationRequest, idempotency_key: str
    ) -> EvaluationRun:
        try:
            async with self.transactions(actor) as repo:
                await repo.authenticate()
                if not 16 <= len(idempotency_key) <= 128:
                    raise ReportError("INVALID_IDEMPOTENCY_KEY", 422)
                replay = await repo.replay_run(request, idempotency_key)
                if replay is not None:
                    return replay
                self.policy.validate(request.provider)
                return await repo.start(
                    request,
                    idempotency_key,
                    self.policy.fingerprint(request.provider),
                    self.policy.budget,
                )
        except ReportError as exc:
            await self.audit_rejection(actor=actor, key=idempotency_key, reason_code=exc.code)
            raise

    async def get(self, *, actor: AuthenticatedActor, run_id: UUID) -> EvaluationRun:
        async with self.transactions(actor) as repo:
            return await repo.read(run_id)

    async def claim(self, *, worker_id: str, organization_id: UUID) -> EvaluationRun | None:
        if not worker_id or len(worker_id) > 100:
            raise ValueError("INVALID_WORKER_ID")
        async with self.transactions(organization_id) as repo:
            return await repo.claim(worker_id)

    async def authorize(self, job: EvaluationRun) -> AuthenticatedActor:
        actor = await self.transactions.resolve(
            organization_id=job.organization_id, membership_id=job.requester_membership_id
        )
        if actor is None:
            raise ReportError("FORBIDDEN", 403)
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            await repo.fence(job)
            dataset = await repo.frozen(job.dataset_version_id)
            if dataset.dataset_hash != job.dataset_hash:
                raise ReportError("EVALUATION_INTEGRITY_FAILED")
        self.policy.validate(job.provider)
        if job.provider_config_hash != self.policy.fingerprint(job.provider):
            raise ReportError("EVALUATION_POLICY_CHANGED", 403)
        return actor

    async def _heartbeat(self, job: EvaluationRun) -> None:
        while True:
            await asyncio.sleep(10)
            async with self.transactions(job.organization_id) as repo:
                await repo.heartbeat(job)

    async def execute(self, job: EvaluationRun) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(job))
        try:
            actor = await self.authorize(job)
            async with self.transactions(actor) as repo:
                dataset = await repo.frozen(job.dataset_version_id)

            async def authorize() -> None:
                await self.authorize(job)

            provider = self.provider or self.policy.build(job, authorize)
            result = await asyncio.wait_for(provider.evaluate(dataset), timeout=300)
            result = self.policy.verify(job, dataset, result)
            actor = await self.authorize(job)
            async with self.transactions(actor) as repo:
                await repo.authenticate()
                await repo.finish(job, result)
        except ReportError as exc:
            if exc.code == "EVALUATION_FENCE_LOST":
                return
            failure = (
                "POLICY"
                if exc.code.startswith("EVALUATION_HOSTED")
                or exc.code == "EVALUATION_POLICY_CHANGED"
                else "AUTHORIZATION"
                if exc.status in (403, 404)
                else "WORKER"
            )
            with suppress(ReportError):
                async with self.transactions(job.organization_id) as repo:
                    await repo.finish(job, None, failure=failure, code=exc.code)
        except Exception:
            with suppress(ReportError):
                async with self.transactions(job.organization_id) as repo:
                    await repo.finish(job, None, failure="WORKER", code="EVALUATION_WORKER_FAILED")
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await heartbeat

    async def run_once(self, *, worker_id: str, organization_id: UUID) -> bool:
        job = await self.claim(worker_id=worker_id, organization_id=organization_id)
        if job is None:
            return False
        if job.status == "RUNNING":
            await self.execute(job)
        return True
