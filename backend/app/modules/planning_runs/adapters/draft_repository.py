"""Tenant/owner-scoped draft persistence using existing transaction infrastructure."""

import hashlib
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.audit.domain.events import AuditOutcome
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.adapters.database_models import MembershipModel
from app.modules.organization.domain.roles import MembershipRole
from app.modules.planning_runs.adapters.database_models import (
    ApprovalModel,
    PlanningDraftModel,
    PlanningDraftVersionModel,
    ProposalModel,
    WorkflowRunModel,
)
from app.modules.planning_runs.application.draft_ports import PlanningDraftRepository
from app.modules.planning_runs.application.draft_service import DraftTransactionFactory
from app.modules.planning_runs.application.ports import PlanningRuntimePort, PlanningRunTransaction
from app.modules.planning_runs.domain.drafts import (
    DraftContent,
    PlanningDraftSnapshot,
    PreparedPlanningAction,
)
from app.modules.planning_runs.domain.models import (
    IdempotencyKeyReusedError,
    OutboxEvent,
    PlanningRunForbiddenError,
    PlanningRunNotFoundError,
    ResourceVersionMismatchError,
    WorkflowCheckpoint,
    WorkflowEvent,
    WorkflowRunStateError,
)
from app.modules.work.adapters.database_models import IdempotencyRecordModel, IdempotencyState


def draft_content_fingerprint(draft: DraftContent) -> str:
    return hashlib.sha256(
        json.dumps(asdict(draft), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


async def lock_planning_workflow(
    session: AsyncSession, organization_id: UUID, run_id: UUID
) -> None:
    key = f"planning-workflow:{organization_id}:{run_id}"
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))


class PostgreSQLPlanningDraftRepository:
    def __init__(
        self,
        transaction: PlanningRunTransaction,
        runtime: PlanningRuntimePort | None = None,
        timezone: str = "Asia/Ho_Chi_Minh",
    ) -> None:
        self._runtime, self._timezone = runtime, timezone
        self._transaction = transaction
        self._session = cast(AsyncSession, transaction.session)

    async def authorize(self, actor: AuthenticatedActor, run_id: UUID) -> None:
        membership = await self._session.scalar(
            select(MembershipModel).where(
                MembershipModel.organization_id == actor.organization_id,
                MembershipModel.id == actor.membership_id,
            )
        )
        if (
            membership is None
            or not membership.is_active
            or membership.role not in {MembershipRole.ADMIN, MembershipRole.MANAGER}
            or membership.role != actor.role
        ):
            raise PlanningRunForbiddenError
        run = await self._session.scalar(
            select(WorkflowRunModel).where(
                WorkflowRunModel.organization_id == actor.organization_id,
                WorkflowRunModel.id == run_id,
                WorkflowRunModel.requested_by_membership_id == actor.membership_id,
            )
        )
        if run is None:
            raise PlanningRunNotFoundError

    async def get_snapshot(
        self, actor: AuthenticatedActor, draft_id: UUID, version: int | None
    ) -> PlanningDraftSnapshot:
        draft = await self._session.scalar(
            select(PlanningDraftModel).where(
                PlanningDraftModel.organization_id == actor.organization_id,
                PlanningDraftModel.id == draft_id,
                PlanningDraftModel.created_by_membership_id == actor.membership_id,
            )
        )
        if draft is None:
            raise PlanningRunNotFoundError
        await self.authorize(actor, draft.workflow_run_id)
        item = await self._session.scalar(
            select(PlanningDraftVersionModel).where(
                PlanningDraftVersionModel.organization_id == actor.organization_id,
                PlanningDraftVersionModel.draft_id == draft_id,
                PlanningDraftVersionModel.version_number
                == (version or draft.current_version_number),
            )
        )
        if item is None:
            raise PlanningRunNotFoundError
        raw = item.content
        content = DraftContent(
            content=raw["content"],
            question=raw.get("question"),
            validation=raw.get("validation", {}),
            versions=raw.get("versions", {}),
            change_summary=raw.get("change_summary"),
        )
        run = await self._session.get(WorkflowRunModel, draft.workflow_run_id)
        proposal = await self._session.scalar(
            select(ProposalModel).where(
                ProposalModel.organization_id == actor.organization_id,
                ProposalModel.workflow_run_id == draft.workflow_run_id,
            )
        )
        can_prepare = (
            item.version_number == draft.current_version_number
            and run is not None
            and run.status in {"DRAFT_AVAILABLE", "WAITING_FOR_DECISION"}
            and (proposal is None or proposal.status not in {"APPROVED", "REJECTED"})
            and not content.validation.get("errors")
            and not content.validation.get("conflicts")
        )
        return PlanningDraftSnapshot(
            draft.id,
            draft.workflow_run_id,
            item.version_number,
            draft.current_version_number,
            item.fingerprint,
            content,
            can_prepare,
            datetime.now(ZoneInfo(self._timezone)).date() if can_prepare else None,
        )

    async def publish(
        self,
        actor: AuthenticatedActor,
        run_id: UUID,
        expected_version: int | None,
        draft: DraftContent,
        request_id: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> PlanningDraftSnapshot:
        await self.authorize(actor, run_id)
        await lock_planning_workflow(self._session, actor.organization_id, run_id)
        await self.authorize(actor, run_id)
        operation = f"planning.draft.publish:{run_id}"
        replay = await self._session.scalar(
            select(IdempotencyRecordModel).where(
                IdempotencyRecordModel.organization_id == actor.organization_id,
                IdempotencyRecordModel.actor_membership_id == actor.membership_id,
                IdempotencyRecordModel.operation == operation,
                IdempotencyRecordModel.idempotency_key == idempotency_key,
            )
        )
        if replay is not None:
            if replay.request_fingerprint != fingerprint:
                raise IdempotencyKeyReusedError
            body = replay.response_body or {}
            return await self.get_snapshot(actor, UUID(str(body["draft_id"])), int(body["version"]))
        run = await self._session.scalar(
            select(WorkflowRunModel)
            .where(
                WorkflowRunModel.organization_id == actor.organization_id,
                WorkflowRunModel.id == run_id,
            )
            .with_for_update()
        )
        if run is None or run.status not in {"RUNNING", "DRAFT_AVAILABLE", "WAITING_FOR_DECISION"}:
            raise WorkflowRunStateError
        current = await self._session.scalar(
            select(PlanningDraftModel)
            .where(
                PlanningDraftModel.organization_id == actor.organization_id,
                PlanningDraftModel.workflow_run_id == run_id,
            )
            .with_for_update()
        )
        decided = await self._session.scalar(
            select(ProposalModel.id).where(
                ProposalModel.organization_id == actor.organization_id,
                ProposalModel.workflow_run_id == run_id,
                ProposalModel.status.in_(["APPROVED", "REJECTED"]),
            )
        )
        if decided is not None:
            raise WorkflowRunStateError("decided plans cannot be changed")
        number = current.current_version_number if current else 0
        if expected_version != (number or None):
            raise ResourceVersionMismatchError(number)
        if current is None:
            current = PlanningDraftModel(
                id=uuid4(),
                organization_id=actor.organization_id,
                workflow_run_id=run_id,
                created_by_membership_id=actor.membership_id,
                current_version_number=1,
                version=1,
            )
            self._session.add(current)
            await self._session.flush()
        else:
            current.current_version_number = number + 1
            current.version += 1
            current.updated_at = datetime.now(UTC)
        item = PlanningDraftVersionModel(
            id=uuid4(),
            organization_id=actor.organization_id,
            draft_id=current.id,
            version_number=number + 1,
            content=asdict(draft),
            fingerprint=draft_content_fingerprint(draft),
        )
        self._session.add(item)
        pending = await self._session.scalars(
            select(ProposalModel)
            .where(
                ProposalModel.organization_id == actor.organization_id,
                ProposalModel.workflow_run_id == run_id,
                ProposalModel.status.in_(["READY_FOR_DECISION", "DRAFT", "VALIDATING"]),
            )
            .with_for_update()
        )
        for proposal in pending:
            if proposal.approval_id is not None:
                approval = await self._session.scalar(
                    select(ApprovalModel)
                    .where(
                        ApprovalModel.organization_id == actor.organization_id,
                        ApprovalModel.id == proposal.approval_id,
                    )
                    .with_for_update()
                )
                if approval is not None and approval.status == "PENDING":
                    approval.status = "SUPERSEDED"
                    approval.version += 1
                    approval.updated_at = datetime.now(UTC)
                proposal.superseded_approval_id = proposal.approval_id
            proposal.approval_id = None
            proposal.status = "STALE"
            proposal.version += 1
            proposal.updated_at = datetime.now(UTC)
        run.status = "DRAFT_AVAILABLE"
        run.version += 1
        run.updated_at = datetime.now(UTC)
        event_type = "planning.draft.created" if number == 0 else "planning.draft.updated"
        if draft.validation.get("conflicts"):
            event_type = "planning.draft.conflicted"
        payload = {
            "draft_id": str(current.id),
            "draft_version": number + 1,
            "fingerprint": item.fingerprint,
            "state": "CONFLICTED" if draft.validation.get("conflicts") else "DRAFT_AVAILABLE",
            "summary": str(
                cast(
                    dict[str, object],
                    cast(dict[str, object], draft.content.get("brief", {})).get("objective", {}),
                ).get("value", "Planning draft")
            ),
            "conflicts": [
                str(cast(dict[str, object], c).get("code", "PLANNING_CONFLICT"))
                for c in cast(list[object], draft.validation.get("conflicts", []))
            ],
        }
        event = await self._transaction.repository.append_event(
            event=WorkflowEvent(
                id=uuid4(),
                organization_id=actor.organization_id,
                workflow_run_id=run_id,
                sequence=0,
                event_type=event_type,
                public_payload=payload,
            )
        )
        await self._transaction.repository.enqueue_outbox_event(
            organization_id=actor.organization_id,
            event=OutboxEvent(
                id=uuid4(),
                organization_id=actor.organization_id,
                event_id=event.id,
                event_type=event_type,
                aggregate_type="workflow_run",
                aggregate_id=run_id,
                payload={
                    **payload,
                    "event_id": str(event.id),
                    "workflow_run_id": str(run_id),
                    "envelope_version": 1,
                },
            ),
        )
        self._session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=actor.organization_id,
                actor_membership_id=actor.membership_id,
                action=event_type,
                outcome=AuditOutcome.SUCCEEDED,
                resource_type="PLANNING_DRAFT",
                resource_id=current.id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                before_data={"version": number},
                after_data={"version": number + 1},
                reason_data={},
            )
        )
        self._session.add(
            IdempotencyRecordModel(
                id=uuid4(),
                organization_id=actor.organization_id,
                actor_membership_id=actor.membership_id,
                operation=operation,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                state=IdempotencyState.COMPLETED,
                response_status=200,
                response_body={"draft_id": str(current.id), "version": number + 1},
                expires_at=datetime.now(UTC) + timedelta(hours=24),
            )
        )
        if draft.versions.get("execution_id"):
            latest = await self._transaction.repository.get_latest_checkpoint(
                actor=actor, run_id=run_id
            )
            await self._transaction.repository.save_checkpoint(
                checkpoint=WorkflowCheckpoint(
                    id=uuid4(),
                    organization_id=actor.organization_id,
                    workflow_run_id=run_id,
                    node="progressive_draft_available",
                    sequence=latest.sequence + 1 if latest else 1,
                    state={
                        "workflow_version": "3.0.0",
                        "schema_version": "planning-draft.v1",
                        "completed_execution_id": draft.versions["execution_id"],
                        "model_calls_used": int(draft.versions.get("model_calls_used", "0")),
                        "locale": draft.versions.get("locale", "en"),
                        "binding": {
                            "draft_id": str(current.id),
                            "draft_version": number + 1,
                            "fingerprint": item.fingerprint,
                        },
                        "question": draft.question,
                    },
                )
            )
        await self._session.flush()
        return await self.get_snapshot(actor, current.id, number + 1)

    async def prepare_action(
        self,
        *,
        actor: AuthenticatedActor,
        draft_id: UUID,
        expected_draft_version: int,
        anchor_date: date,
        request_id: str,
        idempotency_key: str,
    ) -> PreparedPlanningAction:
        if self._runtime is None:
            raise ValueError("draft preparation is unavailable")
        from app.modules.planning_runs.adapters.draft_actions import prepare_draft_action

        return await prepare_draft_action(
            transaction=self._transaction,
            runtime=self._runtime,
            timezone=self._timezone,
            actor=actor,
            draft_id=draft_id,
            expected_draft_version=expected_draft_version,
            anchor_date=anchor_date,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )

    async def audit_rejection(self, actor: AuthenticatedActor, request_id: str, code: str) -> None:
        await self._transaction.repository.audit_rejection(
            actor=actor, action="planning.draft.publish", request_id=request_id, reason_code=code
        )


def build_draft_transaction_factory(
    transaction_factory: object,
    *,
    runtime: PlanningRuntimePort | None = None,
    timezone: str = "Asia/Ho_Chi_Minh",
) -> DraftTransactionFactory:
    from collections.abc import Callable

    transactions = cast(Callable[[AuthenticatedActor], PlanningRunTransaction], transaction_factory)

    @asynccontextmanager
    async def transaction(actor: AuthenticatedActor) -> AsyncGenerator[PlanningDraftRepository]:
        async with transactions(actor) as unit:
            yield PostgreSQLPlanningDraftRepository(unit, runtime, timezone)
            await unit.commit()

    return transaction
