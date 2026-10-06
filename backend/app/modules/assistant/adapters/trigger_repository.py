"""Short tenant-scoped transactions persist a trusted trigger and its ownership."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

from pydantic import TypeAdapter
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.audit.domain.events import AuditOutcome
from app.modules.automations.adapters.delivery_models import SummarySnapshotModel
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.reporting.adapters.database_models import (
    ReportModel,
    ReportSnapshotModel,
    ReportVersionModel,
)
from app.modules.reporting.domain.snapshots import ReportMetricSnapshot, canonical_hash

from ..application.trigger_service import TriggerRepository
from ..domain.models import OrchestrationRun
from ..domain.triggers import ChatTurnTrigger, ExecutionTrigger, SummaryJobTrigger, TriggerError
from .database_models import OrchestrationRunModel
from .repository import orchestration_domain


class SQLTriggerRepository:
    def __init__(self, session: AsyncSession, actor: AuthenticatedActor):
        self.session, self.actor = session, actor

    async def authenticate(self, trigger: ExecutionTrigger) -> None:
        active = await self.session.scalar(
            text(
                "SELECT lock_active_membership(:org,:member) AND EXISTS (SELECT 1 FROM memberships "
                "WHERE organization_id=:org AND id=:member AND user_id=:user "
                "AND (:chat OR role IN ('MANAGER','ADMIN')))"
            ),
            {
                "org": self.actor.organization_id,
                "member": self.actor.membership_id,
                "user": self.actor.user_id,
                "chat": isinstance(trigger, ChatTurnTrigger),
            },
        )
        if active is not True:
            raise TriggerError("FORBIDDEN")

    async def ensure(
        self, trigger: ExecutionTrigger, version: str, fingerprint: str, budget: dict[str, object]
    ) -> OrchestrationRun:
        org = self.actor.organization_id
        if isinstance(trigger, ChatTurnTrigger):
            existing_chat = await self.session.scalar(
                select(OrchestrationRunModel).where(
                    OrchestrationRunModel.organization_id == org,
                    OrchestrationRunModel.turn_id == trigger.turn_id,
                    OrchestrationRunModel.actor_membership_id == self.actor.membership_id,
                    OrchestrationRunModel.trigger_kind == "CHAT_TURN",
                )
            )
            if existing_chat is None:
                raise TriggerError("RESOURCE_NOT_FOUND")
            return orchestration_domain(existing_chat)
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"{org}:{self.actor.membership_id}:report.trigger:{trigger.request_key}"},
        )
        report = await self.session.scalar(
            select(ReportModel).where(
                ReportModel.organization_id == org, ReportModel.id == trigger.report_id
            )
        )
        if report is None:
            raise TriggerError("RESOURCE_NOT_FOUND")
        base = await self.session.scalar(
            select(ReportVersionModel).where(
                ReportVersionModel.organization_id == org,
                ReportVersionModel.report_id == report.id,
                ReportVersionModel.id == trigger.base_version_id,
            )
        )
        if base is None:
            raise TriggerError("REPORT_TRIGGER_VERSION_MISMATCH")
        snapshot = await self.session.scalar(
            select(ReportSnapshotModel).where(
                ReportSnapshotModel.organization_id == org,
                ReportSnapshotModel.report_id == report.id,
                ReportSnapshotModel.id == base.snapshot_id,
            )
        )
        if (
            snapshot is None
            or snapshot.snapshot_hash != trigger.snapshot_hash
            or not ReportMetricSnapshot.model_validate(snapshot.payload).verified_hash()
        ):
            raise TriggerError("REPORT_TRIGGER_SNAPSHOT_MISMATCH")
        summary_id = trigger.summary_id if isinstance(trigger, SummaryJobTrigger) else None
        if summary_id is not None:
            summary = await self.session.scalar(
                select(SummarySnapshotModel).where(
                    SummarySnapshotModel.organization_id == org,
                    SummarySnapshotModel.id == summary_id,
                    SummarySnapshotModel.project_id == report.project_id,
                )
            )
            if summary is None or (
                report.origin == "DAILY_SUMMARY"
                and (
                    report.summary_id != summary_id
                    or report.summary_hash != canonical_hash(summary.payload)
                )
            ):
                raise TriggerError("REPORT_TRIGGER_SUMMARY_MISMATCH")
        identity = canonical_hash(trigger.model_dump(mode="json"))
        existing = await self.session.scalar(
            select(OrchestrationRunModel).where(
                OrchestrationRunModel.organization_id == org,
                OrchestrationRunModel.actor_membership_id == self.actor.membership_id,
                OrchestrationRunModel.request_key == trigger.request_key,
                OrchestrationRunModel.trigger_kind != "CHAT_TURN",
            )
        )
        if existing is not None:
            saved_trigger = existing.execution_plan.get("trigger")
            compatible = (
                TypeAdapter[ExecutionTrigger](ExecutionTrigger)
                .validate_python(saved_trigger)
                .model_dump(mode="json")
                == trigger.model_dump(mode="json")
                if saved_trigger is not None
                else getattr(trigger, "mode", "DRAFT") == "DRAFT"
            )
            if (
                not compatible
                or existing.trigger_kind != trigger.kind
                or existing.report_id != trigger.report_id
                or existing.base_version_id != trigger.base_version_id
                or existing.snapshot_hash != trigger.snapshot_hash
                or existing.summary_id != summary_id
            ):
                raise TriggerError("IDEMPOTENCY_KEY_REUSED")
            return orchestration_domain(existing)
        row = OrchestrationRunModel(
            id=uuid4(),
            organization_id=org,
            turn_id=None,
            trigger_kind=trigger.kind,
            actor_membership_id=self.actor.membership_id,
            project_id=report.project_id,
            report_id=report.id,
            base_version_id=base.id,
            snapshot_id=snapshot.id,
            snapshot_hash=snapshot.snapshot_hash,
            summary_id=summary_id,
            request_key=trigger.request_key,
            orchestrator_version=version,
            orchestrator_fingerprint=fingerprint,
            execution_plan={
                "trigger_fingerprint": identity,
                "trigger": trigger.model_dump(mode="json"),
                "locale": report.locale,
            },
            budget=budget,
            checkpoint={},
            usage={},
            status="QUEUED",
        )
        self.session.add(row)
        self.session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=org,
                actor_membership_id=self.actor.membership_id,
                action="report.orchestration.requested",
                outcome=AuditOutcome.SUCCEEDED,
                resource_type="report",
                resource_id=report.id,
                request_id=str(uuid4()),
                idempotency_key=trigger.request_key,
                before_data={},
                after_data={"orchestration_run_id": str(row.id), "trigger_kind": trigger.kind},
                reason_data={},
            )
        )
        await self.session.flush()
        return orchestration_domain(row)

    async def start(self, run_id: UUID) -> None:
        row = await self.session.scalar(
            select(OrchestrationRunModel)
            .where(
                OrchestrationRunModel.organization_id == self.actor.organization_id,
                OrchestrationRunModel.id == run_id,
                OrchestrationRunModel.actor_membership_id == self.actor.membership_id,
                OrchestrationRunModel.trigger_kind != "CHAT_TURN",
            )
            .with_for_update()
        )
        if row is None:
            raise TriggerError("RESOURCE_NOT_FOUND")
        if row.status in ("QUEUED", "RUNNING"):
            row.status = "RUNNING"
            row.started_at = row.started_at or cast(
                datetime, await self.session.scalar(select(func.clock_timestamp()))
            )
            row.updated_at = cast(
                datetime, await self.session.scalar(select(func.clock_timestamp()))
            )

    async def finish(self, run_id: UUID, *, succeeded: bool, usage: dict[str, int]) -> None:
        row = await self.session.scalar(
            select(OrchestrationRunModel)
            .where(
                OrchestrationRunModel.organization_id == self.actor.organization_id,
                OrchestrationRunModel.id == run_id,
                OrchestrationRunModel.actor_membership_id == self.actor.membership_id,
                OrchestrationRunModel.trigger_kind != "CHAT_TURN",
            )
            .with_for_update()
        )
        if row is None:
            raise TriggerError("RESOURCE_NOT_FOUND")
        row.status = "AWAITING_HUMAN" if succeeded else "FAILED"
        row.usage = usage
        row.stop_reason = "AWAITING_MANAGER_REVIEW" if succeeded else "REPORTING_UNAVAILABLE"
        row.safe_error_code = None if succeeded else "REPORTING_UNAVAILABLE"
        at = cast(datetime, await self.session.scalar(select(func.clock_timestamp())))
        row.completed_at = row.completed_at or at
        row.updated_at = at

    async def reject(self, trigger: ExecutionTrigger, reason: str) -> None:
        self.session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=self.actor.organization_id,
                actor_membership_id=self.actor.membership_id,
                action="report.orchestration.requested",
                outcome=AuditOutcome.REJECTED,
                resource_type="assistant_turn"
                if isinstance(trigger, ChatTurnTrigger)
                else "report",
                resource_id=trigger.turn_id
                if isinstance(trigger, ChatTurnTrigger)
                else trigger.report_id,
                request_id=str(uuid4()),
                idempotency_key=None
                if isinstance(trigger, ChatTurnTrigger)
                else trigger.request_key,
                before_data={},
                after_data={},
                reason_data={"reason_code": reason},
            )
        )
        await self.session.flush()


class TriggerTransactions:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self.sessions = sessions

    @asynccontextmanager
    async def __call__(self, actor: AuthenticatedActor) -> AsyncGenerator[TriggerRepository]:
        async with self.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true), "
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            yield cast(TriggerRepository, SQLTriggerRepository(session, actor))
