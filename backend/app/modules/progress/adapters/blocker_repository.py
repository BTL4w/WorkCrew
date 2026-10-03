"""Current-assignee/Manager authorization and one SQL transaction per mutation."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.audit.domain.events import AuditOutcome
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.planning_runs.adapters.database_models import OutboxEventModel
from app.modules.progress.adapters.blocker_models import (
    BlockerEvidenceModel,
    BlockerModel,
    BlockerTransitionModel,
)
from app.modules.progress.adapters.daily_update_repository import SqlAlchemyDailyUpdateRepository
from app.modules.progress.adapters.evidence_models import EvidenceOriginalModel
from app.modules.progress.application.blocker_service import BlockerRepository
from app.modules.progress.domain.blockers import (
    Blocker,
    BlockerCommand,
    BlockerError,
    BlockerTransition,
    transition_blocker,
)
from app.modules.progress.domain.daily_updates import DailyUpdateError
from app.modules.work.adapters.database_models import TaskModel


class SqlAlchemyBlockerRepository(SqlAlchemyDailyUpdateRepository):
    async def authenticate(self) -> None:
        try:
            await super().authenticate()
        except DailyUpdateError as exc:
            raise BlockerError(exc.code, exc.status) from exc

    async def replay(self, operation: str, key: str, fingerprint: str) -> dict[str, object] | None:
        try:
            return await super().replay(operation, key, fingerprint)
        except DailyUpdateError as exc:
            raise BlockerError(exc.code, exc.status) from exc

    async def permitted_task(self, task_id: UUID, *, lock: bool = False) -> TaskModel:
        query = select(TaskModel).where(
            TaskModel.organization_id == self.org, TaskModel.id == task_id
        )
        task = await self.session.scalar(query.with_for_update() if lock else query)
        allowed = await self.session.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM memberships WHERE organization_id=:org "
                "AND id=:member AND role IN ('MANAGER','ADMIN'))"
            ),
            {"org": self.org, "member": self.actor.membership_id},
        )
        if task is None or (
            not allowed and task.assignee_membership_id != self.actor.membership_id
        ):
            raise BlockerError("RESOURCE_NOT_FOUND", 404)
        return task

    async def authorize(self, task_id: UUID) -> None:
        await self.permitted_task(task_id, lock=True)

    async def apply(self, command: BlockerCommand, update_id: UUID | None = None) -> Blocker:
        task = await self.permitted_task(command.task_id, lock=True)
        if task.version != command.expected_task_version:
            raise BlockerError("STALE_TASK")
        row = None
        if command.blocker_id:
            row = await self.session.scalar(
                select(BlockerModel)
                .where(
                    BlockerModel.organization_id == self.org, BlockerModel.id == command.blocker_id
                )
                .with_for_update()
            )
            if row is None:
                raise BlockerError("RESOURCE_NOT_FOUND", 404)
        at = datetime.now(UTC)
        result, event = transition_blocker(
            Blocker.model_validate(row.payload) if row else None,
            command,
            self.actor.membership_id,
            at,
        )
        if command.action in {"CREATE", "EDIT"}:
            for ref in sorted(command.evidence_refs, key=lambda r: r.evidence_id):
                await self.lock(f"evidence:{ref.evidence_id}")
                original = await self.session.scalar(
                    select(EvidenceOriginalModel).where(
                        EvidenceOriginalModel.organization_id == self.org,
                        EvidenceOriginalModel.id == ref.evidence_id,
                        EvidenceOriginalModel.version == ref.version,
                    )
                )
                if (
                    original is None
                    or original.state != "READY"
                    or (original.confirmed_at is None and original.expires_at <= at)
                ):
                    raise BlockerError("INVALID_EVIDENCE", 422)
                if original.uploader_membership_id != self.actor.membership_id:
                    related = await self.session.scalar(
                        select(BlockerEvidenceModel.evidence_id)
                        .join(
                            BlockerModel,
                            (BlockerModel.organization_id == BlockerEvidenceModel.organization_id)
                            & (BlockerModel.id == BlockerEvidenceModel.blocker_id),
                        )
                        .where(
                            BlockerModel.task_id == task.id,
                            BlockerEvidenceModel.organization_id == self.org,
                            BlockerEvidenceModel.evidence_id == ref.evidence_id,
                            BlockerEvidenceModel.evidence_version == ref.version,
                        )
                        .limit(1)
                    )
                    if related is None:
                        raise BlockerError("UNRELATED_EVIDENCE", 422)
                if (
                    original.uploader_membership_id == self.actor.membership_id
                    and original.confirmed_at is None
                ):
                    original.confirmed_at = at
        if row is None:
            row = BlockerModel(
                id=result.id,
                organization_id=self.org,
                task_id=result.task_id,
                created_by_membership_id=result.created_by_membership_id,
                created_at=result.created_at,
            )
            self.session.add(row)
        row.version, row.severity, row.status, row.archived = (
            result.version,
            result.severity,
            result.status,
            result.archived,
        )
        row.payload, row.updated_at = result.model_dump(mode="json"), at
        await self.session.flush()
        self.session.add(
            BlockerTransitionModel(
                id=event.id,
                organization_id=self.org,
                blocker_id=result.id,
                version=result.version,
                actor_membership_id=self.actor.membership_id,
                update_id=update_id,
                payload=event.model_dump(mode="json"),
                at=at,
            )
        )
        await self.session.flush()
        for ref in result.evidence_refs:
            self.session.add(
                BlockerEvidenceModel(
                    organization_id=self.org,
                    blocker_id=result.id,
                    blocker_version=result.version,
                    evidence_id=ref.evidence_id,
                    evidence_version=ref.version,
                )
            )
        event_id = uuid4()
        self.session.add(
            OutboxEventModel(
                id=event_id,
                event_id=event_id,
                organization_id=self.org,
                event_type="blocker.changed.v1",
                aggregate_type="blocker",
                aggregate_id=result.id,
                envelope_version="1.0",
                payload={
                    "blocker_id": str(result.id),
                    "task_id": str(task.id),
                    "version": result.version,
                    "status": result.status,
                    "severity": result.severity,
                },
            )
        )
        await self.session.flush()
        return result

    async def list(self, task_id: UUID) -> tuple[Blocker, ...]:
        await self.permitted_task(task_id)
        rows = await self.session.scalars(
            select(BlockerModel)
            .where(BlockerModel.organization_id == self.org, BlockerModel.task_id == task_id)
            .order_by(BlockerModel.created_at, BlockerModel.id)
        )
        return tuple(Blocker.model_validate(row.payload) for row in rows)

    async def lifecycle_history(self, blocker_id: UUID) -> tuple[BlockerTransition, ...]:
        row = await self.session.scalar(
            select(BlockerModel).where(
                BlockerModel.organization_id == self.org, BlockerModel.id == blocker_id
            )
        )
        if row is None:
            raise BlockerError("RESOURCE_NOT_FOUND", 404)
        await self.permitted_task(row.task_id)
        rows = await self.session.scalars(
            select(BlockerTransitionModel)
            .where(
                BlockerTransitionModel.organization_id == self.org,
                BlockerTransitionModel.blocker_id == blocker_id,
            )
            .order_by(BlockerTransitionModel.version)
        )
        return tuple(BlockerTransition.model_validate(row.payload) for row in rows)

    async def audit(
        self,
        action: str,
        request_id: str,
        key: str | None,
        resource_id: UUID | None,
        code: str | None = None,
    ) -> None:
        self.session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id,
                action=action,
                outcome=AuditOutcome.REJECTED if code else AuditOutcome.SUCCEEDED,
                resource_type="blocker",
                resource_id=resource_id,
                request_id=request_id,
                idempotency_key=key,
                before_data={},
                after_data={},
                reason_data={"code": code} if code else {},
            )
        )


class SqlAlchemyBlockerTransactions:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self.sessions = sessions

    @asynccontextmanager
    async def __call__(self, actor: AuthenticatedActor) -> AsyncGenerator[BlockerRepository]:
        async with self.sessions.begin() as session:
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true), "
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            yield SqlAlchemyBlockerRepository(session, actor)
