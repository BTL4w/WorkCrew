"""Lease one Assistant job without holding its claim transaction during execution."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from app.modules.assistant.application.ports import AssistantTransaction
from app.modules.assistant.domain.models import AssistantJob


class AssistantJobHandler(Protocol):
    async def __call__(self, *, job: AssistantJob, worker_id: str) -> None: ...


def normalize_job_error(error: Exception) -> str:
    """Never persist provider text or exception bodies in a durable job."""
    code = getattr(error, "safe_error_code", None)
    if isinstance(code, str) and code.replace("_", "").isalnum() and code.isupper():
        return code[:100]
    return "ASSISTANT_EXECUTION_FAILED"


class AssistantJobService:
    def __init__(
        self,
        *,
        transaction_factory: Callable[[UUID], AssistantTransaction],
        handler: AssistantJobHandler,
        organization_scopes: set[UUID],
        lease_seconds: int = 60,
        job_type: str = "assistant.turn.execute",
    ) -> None:
        self._transactions = transaction_factory
        self._handler = handler
        self._scopes = organization_scopes
        self._lease_seconds = lease_seconds
        self._job_type = job_type

    async def run_once(self, *, worker_id: str, organization_id: UUID) -> bool:
        if organization_id not in self._scopes:
            raise ValueError("ASSISTANT_WORKER_TENANT_SCOPE_VIOLATION")
        now = datetime.now(UTC)
        async with self._transactions(organization_id) as txn:
            job = await txn.repository.claim_job(
                organization_id=organization_id,
                worker_id=worker_id,
                now=now,
                lease_until=now + timedelta(seconds=self._lease_seconds),
                job_type=self._job_type,
            )
            if job is None:
                return False
            await txn.commit()
        try:
            await self._handler(job=job, worker_id=worker_id)
        except Exception as error:
            async with self._transactions(organization_id) as txn:
                await txn.repository.fail_job(
                    job_id=job.id,
                    worker_id=worker_id,
                    error_code=normalize_job_error(error),
                    next_available_at=now + timedelta(seconds=5),
                )
                await txn.commit()
        else:
            async with self._transactions(organization_id) as txn:
                await txn.repository.complete_job(job_id=job.id, worker_id=worker_id)
                await txn.commit()
        return True
