"""Release SQL locks before one bounded model call, then revalidate context."""

import asyncio
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.progress.domain.blockers import BlockerError
from app.modules.risk.application.ports import RiskAssessmentPort, RiskTransactionPort
from app.modules.risk.domain.assessments import RiskAssessment, risk_band, validate_risk_judgment


def validate_key(key: str) -> None:
    if not 16 <= len(key) <= 128:
        raise BlockerError("IDEMPOTENCY_KEY_REQUIRED", 400)


class RiskService:
    def __init__(self, transactions: RiskTransactionPort, model: RiskAssessmentPort):
        self.transactions = transactions
        self.model = model

    async def refresh(
        self, actor: AuthenticatedActor, task_id: UUID, cause_event_id: UUID
    ) -> RiskAssessment:
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            return await repo.enqueue(task_id, cause_event_id)

    async def request_refresh(
        self, actor: AuthenticatedActor, task_id: UUID, key: str
    ) -> RiskAssessment:
        validate_key(key)
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            await repo.authorize(task_id)
            replay = await repo.replay("risk.refresh", key, str(task_id))
            if replay:
                return RiskAssessment.model_validate(replay)
            cause = uuid5(NAMESPACE_URL, f"{actor.organization_id}:{actor.membership_id}:{key}")
            result = await repo.enqueue(task_id, cause)
            await repo.remember("risk.refresh", key, str(task_id), result.model_dump(mode="json"))
            return result

    async def current(self, actor: AuthenticatedActor, task_id: UUID) -> RiskAssessment | None:
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            return await repo.current(task_id)

    async def run_once(self, actor: AuthenticatedActor) -> bool:
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            lease = await repo.claim()
        if lease is None:
            return False
        result = RiskAssessment(
            id=lease.id,
            task_id=lease.task_id,
            task_version=lease.inputs.task_version,
            state="UNAVAILABLE",
            evaluated_at=datetime.now(UTC),
            input_snapshot=lease.inputs,
            limitation="MODEL_UNAVAILABLE",
        )
        try:
            judgment, model_ref = await asyncio.wait_for(
                self.model.assess(lease.inputs), timeout=20
            )
            judgment = validate_risk_judgment(lease.inputs, judgment)
            result = result.model_copy(
                update={
                    "judgment": judgment,
                    "band": risk_band(judgment.score),
                    "state": "READY" if judgment.score is not None else "UNAVAILABLE",
                    "limitation": "" if judgment.score is not None else "INSUFFICIENT_CONTEXT",
                    "model_ref": model_ref,
                }
            )
        except Exception:
            pass  # Safe public failure; no provider traces or invented numeric fallback.
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            await repo.finish(lease, result)
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            await repo.notify(result.id)
        return True

    async def audit_transport_rejection(
        self, *, actor: AuthenticatedActor, request_id: str, key: str | None, reason_code: str
    ) -> None:
        async with self.transactions(actor) as repo:
            await repo.audit(
                "risk.rejected",
                request_id,
                key if key and len(key) <= 128 else None,
                None,
                reason_code,
            )
