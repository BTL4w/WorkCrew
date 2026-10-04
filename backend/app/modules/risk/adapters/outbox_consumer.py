"""Typed outbox dispatch and bounded tenant reconciliation, without model calls in dispatch."""

from typing import Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.automations.domain.digests import SummaryCaptured, SummaryDelivered
from app.modules.automations.domain.schedules import ScheduleChanged
from app.modules.identity.adapters.current_actor import CurrentActorResolver
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.planning_runs.domain.models import OutboxEvent
from app.modules.progress.domain.blockers import BlockerError
from app.modules.reporting.domain.events import MetricsCaptured, ReportPublished
from app.modules.risk.adapters.repository import RiskRepository, input_hash
from app.modules.risk.application.notification_service import NotificationService
from app.modules.risk.application.risk_service import RiskService
from app.modules.work.adapters.database_models import TaskModel
from app.modules.work.planning.adapters.database_models import TaskDependencyModel


class RiskEventRunner(Protocol):
    async def queue_event(self, event: OutboxEvent) -> None: ...
    async def deliver(self, event: OutboxEvent) -> None: ...


class RiskOutboxPublisher:
    def __init__(self, runner: RiskEventRunner):
        self.runner = runner

    async def publish(self, event: OutboxEvent) -> None:
        if event.envelope_version != "1.0":
            raise ValueError("Unsupported risk event envelope")
        if event.event_type == "risk.assessed.v1":
            UUID(str(event.payload["risk_id"]))
            await self.runner.deliver(event)
        elif event.event_type == "weekly_plan.captured":
            UUID(str(event.payload["project_week_id"]))
            await self.runner.queue_event(event)
        elif event.event_type in {
            "daily_update.confirmed",
            "blocker.changed.v1",
            "task.assigned.v1",
        }:
            values = (
                event.payload["task_ids"]
                if event.event_type == "daily_update.confirmed"
                else [event.payload["task_id"]]
            )
            if not isinstance(values, list) or len(cast(list[object], values)) > 100:
                raise ValueError("Invalid risk trigger tasks")
            for value in cast(list[object], values):
                UUID(str(value))
            await self.runner.queue_event(event)
        elif event.event_type == "automation.summary.captured.v1":
            captured = SummaryCaptured.model_validate(event.payload)
            if event.aggregate_type != "daily_summary" or captured.summary_id != event.aggregate_id:
                raise ValueError("Invalid summary event aggregate")
        elif event.event_type == "automation.summary.delivered.v1":
            delivered = SummaryDelivered.model_validate(event.payload)
            if (
                event.aggregate_type != "daily_summary_delivery"
                or delivered.delivery_id != event.aggregate_id
            ):
                raise ValueError("Invalid delivery event aggregate")
        elif event.event_type == "automation.schedule.changed.v1":
            change = ScheduleChanged.model_validate(event.payload)
            if (
                event.aggregate_type != "daily_summary_schedule"
                or change.schedule_id != event.aggregate_id
            ):
                raise ValueError("Invalid schedule event aggregate")
            # PostgreSQL reconciliation owns delivery; this event records confirmed configuration.
        elif event.event_type == "report.metrics_captured.v1":
            captured_report = MetricsCaptured.model_validate(event.payload)
            if event.aggregate_type != "report" or captured_report.report_id != event.aggregate_id:
                raise ValueError("Invalid report event aggregate")
            # Fact-only acknowledgement; narrative generation has a separate bounded job.
        elif event.event_type == "report.published.v1":
            published_report = ReportPublished.model_validate(event.payload)
            if event.aggregate_type != "report" or published_report.report_id != event.aggregate_id:
                raise ValueError("Invalid report publication aggregate")
        elif event.event_type in {"risk.review_recorded.v1", "risk.notification_read.v1"}:
            UUID(str(event.aggregate_id))
        else:
            raise NotImplementedError(f"Publisher not implemented for {event.event_type}")


class RiskWorker:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        service: RiskService,
        resolver: CurrentActorResolver,
    ):
        self.sessions = sessions
        self.service = service
        self.resolver = resolver
        self.cursors: dict[UUID, UUID | None] = {}

    async def managers(self, organization_id: UUID) -> list[AuthenticatedActor]:
        async with self.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text("SELECT set_config('app.organization_id',:org,true)"),
                {"org": str(organization_id)},
            )
            ids = (
                await session.scalars(
                    text(
                        "SELECT m.id FROM memberships m JOIN users u ON u.id=m.user_id "
                        "WHERE m.organization_id=:org AND m.is_active AND u.is_active "
                        "AND m.role IN ('MANAGER','ADMIN') ORDER BY m.id LIMIT 100"
                    ),
                    {"org": organization_id},
                )
            ).all()
        actors: list[AuthenticatedActor] = []
        for id in ids:
            actor = await self.resolver.resolve(organization_id=organization_id, membership_id=id)
            if actor and actor.role in (MembershipRole.MANAGER, MembershipRole.ADMIN):
                actors.append(actor)
        return actors

    async def queue_event(self, event: OutboxEvent) -> None:
        actors = await self.managers(event.organization_id)
        if not actors:
            return  # Reconciliation resumes when a current authorized Manager exists.
        actor = actors[0]
        if event.event_type == "weekly_plan.captured":
            async with self.service.transactions(actor) as repo:
                await repo.authenticate()
                assert isinstance(repo, RiskRepository)
                ids = (
                    await repo.session.scalars(
                        select(TaskModel.id)
                        .where(
                            TaskModel.organization_id == event.organization_id,
                            TaskModel.project_week_id
                            == UUID(str(event.payload["project_week_id"])),
                        )
                        .limit(100)
                    )
                ).all()
        else:
            ids = (
                event.payload["task_ids"]
                if event.event_type == "daily_update.confirmed"
                else [event.payload["task_id"]]
            )
        tasks = {UUID(str(id)) for id in ids}
        async with self.service.transactions(actor) as repo:
            await repo.authenticate()
            assert isinstance(repo, RiskRepository)
            successors = (
                await repo.session.scalars(
                    select(TaskDependencyModel.successor_task_id)
                    .where(
                        TaskDependencyModel.organization_id == event.organization_id,
                        TaskDependencyModel.predecessor_task_id.in_(tasks),
                    )
                    .limit(100)
                )
            ).all()
            tasks.update(successors)
        for task in sorted(tasks):
            try:
                await self.service.refresh(actor, task, event.event_id)
            except BlockerError as exc:
                if exc.status != 404:
                    raise

    async def deliver(self, event: OutboxEvent) -> None:
        for actor in await self.managers(event.organization_id):
            try:
                await NotificationService(self.service.transactions).deliver(
                    actor, UUID(str(event.payload["risk_id"]))
                )
            except BlockerError as exc:
                if exc.status not in (403, 404):
                    raise

    async def run_once(self, *, worker_id: str, organization_id: UUID) -> bool:
        actors = await self.managers(organization_id)
        if not actors:
            return False
        actor = actors[0]
        async with self.service.transactions(actor) as repo:
            await repo.authenticate()
            assert isinstance(repo, RiskRepository)
            query = select(TaskModel.id).where(TaskModel.organization_id == organization_id)
            cursor = self.cursors.get(organization_id)
            if cursor:
                query = query.where(TaskModel.id > cursor)
            tasks = (await repo.session.scalars(query.order_by(TaskModel.id).limit(25))).all()
            self.cursors[organization_id] = tasks[-1] if tasks else None
        # A context failure is local to one Task. Each enqueue gets its own
        # transaction so an oversized Task cannot roll back the rest of the page.
        for task in tasks:
            async with self.service.transactions(actor) as repo:
                await repo.authenticate()
                assert isinstance(repo, RiskRepository)
                try:
                    inputs = await repo.inputs(task)
                    fingerprint = input_hash(inputs)
                except BlockerError as exc:
                    if exc.code != "RISK_CONTEXT_LIMIT":
                        raise
                    source = await repo.permitted_task(task)
                    fingerprint = f"context-limit:v{source.version}"
                cause = uuid5(NAMESPACE_URL, f"risk:{organization_id}:{task}:{fingerprint}")
                await repo.enqueue(task, cause)
        return await self.service.run_once(actor)
