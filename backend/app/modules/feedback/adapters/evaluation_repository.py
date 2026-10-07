"""Tenant-scoped job claims and append-only case measurements."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.audit.domain.events import AuditOutcome
from app.modules.identity.adapters.auth_repository import SqlAlchemyAuthTransactionFactory
from app.modules.identity.application.current_actor_service import CurrentActorService
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.planning_runs.adapters.database_models import OutboxEventModel
from app.modules.reporting.domain.reports import ReportError
from app.modules.reporting.domain.snapshots import canonical_hash

from ..domain.evaluation import ReportEvaluationResult
from ..domain.evaluation_runs import PROVIDER_POLICY, EvaluationRequest, EvaluationRun
from .curation_repository import SQLCurationRepository
from .evaluation_models import EvaluationResultModel as ResultRow
from .evaluation_models import EvaluationRunModel as RunRow


def run_domain(row: RunRow) -> EvaluationRun:
    values = {key: getattr(row, key) for key in EvaluationRun.model_fields if hasattr(row, key)}
    values["result"] = row.summary
    return EvaluationRun.model_validate(values)


class SQLEvaluationRepository(SQLCurationRepository):
    async def evidence(
        self, operation: str, key: str, identity: UUID | None, code: str | None = None
    ) -> None:
        self.session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id
                if self.actor.membership_id.int
                else None,
                action=f"evaluation.{operation}",
                outcome=AuditOutcome.REJECTED if code else AuditOutcome.SUCCEEDED,
                resource_type="evaluation_run",
                resource_id=identity,
                request_id=str(uuid4()),
                idempotency_key=key or None,
                before_data={},
                after_data={},
                reason_data={"reason_code": code} if code else {"policy_version": PROVIDER_POLICY},
            )
        )
        if code is None:
            self.session.add(
                OutboxEventModel(
                    id=uuid4(),
                    organization_id=self.org,
                    event_id=uuid4(),
                    event_type="evaluation.run.requested.v1"
                    if operation == "run.started"
                    else "evaluation.run.completed.v1",
                    aggregate_type="evaluation",
                    aggregate_id=identity,
                    payload={
                        "schema_version": "1.0",
                        "id": str(identity),
                        "policy_version": PROVIDER_POLICY,
                    },
                    status="PENDING",
                )
            )

    async def run_row(self, identity: UUID, *, lock: bool = False) -> RunRow:
        query = select(RunRow).where(RunRow.organization_id == self.org, RunRow.id == identity)
        if lock:
            query = query.with_for_update()
        row = await self.session.scalar(query)
        if row is None:
            raise ReportError("RESOURCE_NOT_FOUND", 404)
        return row

    async def read(self, identity: UUID) -> EvaluationRun:
        await self.authenticate()
        row = await self.run_row(identity)
        dataset = await self.frozen(row.dataset_version_id)
        if dataset.dataset_hash != row.dataset_hash:
            raise ReportError("EVALUATION_INTEGRITY_FAILED")
        return run_domain(row)

    async def replay_run(self, request: EvaluationRequest, key: str) -> EvaluationRun | None:
        await self.authenticate()
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"{self.org}:{self.actor.membership_id}:evaluation-run:{key}"},
        )
        row = await self.session.scalar(
            select(RunRow).where(
                RunRow.organization_id == self.org,
                RunRow.requester_membership_id == self.actor.membership_id,
                RunRow.request_key == key,
            )
        )
        if row is None:
            return None
        if row.request_hash != canonical_hash(request.model_dump(mode="json")):
            raise ReportError("IDEMPOTENCY_KEY_REUSED", 409)
        return (await self.read(row.id)).model_copy(update={"replayed": True})

    async def start(
        self, request: EvaluationRequest, key: str, config_hash: str, budget: int
    ) -> EvaluationRun:
        dataset = await self.frozen(request.dataset_version_id)
        row = RunRow(
            id=uuid4(),
            organization_id=self.org,
            requester_membership_id=self.actor.membership_id,
            request_key=key,
            request_hash=canonical_hash(request.model_dump(mode="json")),
            dataset_version_id=dataset.id,
            dataset_version=dataset.version,
            dataset_hash=dataset.dataset_hash,
            dataset_policy_version=dataset.policy_version,
            provider=request.provider,
            provider_policy_version=PROVIDER_POLICY,
            provider_config_hash=config_hash,
            budget_tokens=budget,
            status="QUEUED",
            fence=0,
            attempts=0,
            created_at=await self.captured_at(),
        )
        self.session.add(row)
        await self.session.flush()
        await self.evidence("run.started", key, row.id)
        return run_domain(row)

    async def claim(self, worker: str) -> EvaluationRun | None:
        at = await self.captured_at()
        row = await self.session.scalar(
            select(RunRow)
            .where(
                RunRow.organization_id == self.org,
                or_(
                    RunRow.status == "QUEUED",
                    (RunRow.status == "RUNNING") & (RunRow.lease_until <= at),
                ),
            )
            .order_by(RunRow.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        if row.attempts >= 3 or (row.status == "RUNNING" and row.provider == "hosted"):
            row.fence += 1
            row.status = "FAILED"
            row.failure_kind = "WORKER"
            row.safe_error_code = "EVALUATION_LEASE_EXPIRED"
            row.lease_owner = row.lease_until = None
            await self.evidence("run.completed", "worker", row.id)
            return run_domain(row)
        row.status = "RUNNING"
        row.fence += 1
        row.attempts += 1
        row.lease_owner = worker
        row.lease_until = at + timedelta(seconds=30)
        return run_domain(row)

    async def fence(self, job: EvaluationRun) -> RunRow:
        row = await self.run_row(job.id, lock=True)
        if (
            row.status != "RUNNING"
            or row.fence != job.fence
            or row.lease_owner != job.lease_owner
            or row.lease_until is None
            or row.lease_until <= await self.captured_at()
        ):
            raise ReportError("EVALUATION_FENCE_LOST", 409)
        return row

    async def heartbeat(self, job: EvaluationRun) -> None:
        row = await self.fence(job)
        row.lease_until = await self.captured_at() + timedelta(seconds=30)

    async def finish(
        self,
        job: EvaluationRun,
        result: ReportEvaluationResult | None,
        *,
        failure: str | None = None,
        code: str | None = None,
    ) -> None:
        row = await self.fence(job)
        if result is not None:
            dataset = await self.frozen(job.dataset_version_id)
            for case, measurement in zip(dataset.cases, result.cases, strict=True):
                self.session.add(
                    ResultRow(
                        id=uuid4(),
                        organization_id=self.org,
                        run_id=job.id,
                        dataset_version_id=dataset.id,
                        case_id=case.id,
                        case_version=case.version,
                        case_hash=case.case_hash,
                        measurement=measurement,
                    )
                )
            row.summary = result.model_dump(mode="json")
            row.status = "PASSED" if result.gate_passed else "FAILED"
            row.failure_kind = None if result.gate_passed else "GATE"
            row.safe_error_code = None if result.gate_passed else "EVALUATION_GATE_FAILED"
        else:
            row.status = "CANCELLED" if failure in ("AUTHORIZATION", "POLICY") else "FAILED"
            row.failure_kind = failure
            row.safe_error_code = code
        row.lease_owner = row.lease_until = None
        await self.evidence("run.completed", "worker", row.id)
        await self.session.flush()


class EvaluationTransactions:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], timezone: str):
        self.sessions, self.timezone = sessions, timezone

    async def resolve(
        self, *, organization_id: UUID, membership_id: UUID
    ) -> AuthenticatedActor | None:
        return await CurrentActorService(SqlAlchemyAuthTransactionFactory(self.sessions)).resolve(
            organization_id=organization_id, membership_id=membership_id
        )

    @asynccontextmanager
    async def __call__(
        self, actor: AuthenticatedActor | UUID
    ) -> AsyncGenerator[SQLEvaluationRepository]:
        async with self.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            org = actor.organization_id if isinstance(actor, AuthenticatedActor) else actor
            member = str(actor.membership_id) if isinstance(actor, AuthenticatedActor) else ""
            await session.execute(
                text(
                    "SELECT "
                    "set_config('app.organization_id',:org,true),set_config('app.membership_id',:member,true)"
                ),
                {"org": str(org), "member": member},
            )
            # Worker paths use job metadata only; this actor cannot authorize a dataset.
            worker_actor = (
                actor
                if isinstance(actor, AuthenticatedActor)
                else AuthenticatedActor(
                    UUID(int=0), "", "", UUID(int=0), org, "", MembershipRole.EMPLOYEE
                )
            )
            yield SQLEvaluationRepository(session, worker_actor, self.timezone)
