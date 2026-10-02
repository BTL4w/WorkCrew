"""Background worker entrypoint for processing jobs and outbox events."""

import asyncio
import contextlib
import logging
import signal
import sys
from typing import NoReturn, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.modules.assistant.adapters.agent_runtime import (
    AgentRecordingModelGateway,
    AssistantAgentRuntime,
    AssistantTurnExecutor,
    build_agent_registry,
    build_execution_engine_factory,
)
from app.modules.assistant.adapters.assignment_tools import (
    AssignmentApplicationService,
    AssistantAssignmentContextResolver,
    AssistantAssignmentToolAdapter,
)
from app.modules.assistant.adapters.daily_update_tools import (
    DailyUpdateContextResolver,
    DailyUpdateToolAdapter,
)
from app.modules.assistant.adapters.planning_tools import AssistantPlanningToolAdapter
from app.modules.assistant.adapters.title_gateway import build_title_gateway
from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory
from app.modules.assistant.adapters.work_tools import RecordingToolExecutor, WorkToolExecutor
from app.modules.assistant.application.execution_service import AssistantExecutionService
from app.modules.assistant.application.job_service import AssistantJobService
from app.modules.assistant.application.projection_service import AssistantProjectionService
from app.modules.assistant.application.title_service import ConversationTitleService
from app.modules.identity.adapters.auth_repository import SqlAlchemyAuthTransactionFactory
from app.modules.identity.adapters.current_actor import CurrentActorResolver
from app.modules.identity.application.current_actor_service import CurrentActorService
from app.modules.organization.adapters.member_repository import SqlAlchemyMemberTransactionFactory
from app.modules.organization.application.member_service import MemberService
from app.modules.people_capacity.adapters.repository import (
    SqlAlchemyPeopleCapacityTransactionFactory,
)
from app.modules.people_capacity.application.service import PeopleCapacityService
from app.modules.planning_runs.adapters.ai_runtime import (
    PlanningAIRuntime,
    build_model_gateway,
    build_planning_job_handlers,
)
from app.modules.planning_runs.adapters.transaction import PostgreSQLPlanningRunTransactionFactory
from app.modules.planning_runs.application.job_service import JobService
from app.modules.planning_runs.application.outbox_service import (
    OutboxService,
    UnsupportedOutboxPublisher,
)
from app.modules.planning_runs.application.proposal_service import ProposalService
from app.modules.planning_runs.application.run_service import PlanningRunService
from app.modules.progress.adapters.daily_update_runtime import (
    build_daily_services,
    image_token_bound,
)
from app.modules.progress.adapters.evidence_repository import SqlAlchemyEvidenceTransactionFactory
from app.modules.progress.adapters.filesystem_storage import FilesystemEvidenceStorage
from app.modules.progress.application.evidence_service import EvidenceService
from app.modules.work.adapters.project_repository import SqlAlchemyProjectTransactionFactory
from app.modules.work.adapters.task_repository import SqlAlchemyTaskTransactionFactory
from app.modules.work.application.project_service import ProjectService
from app.modules.work.application.task_service import TaskService
from app.modules.work.planning.adapters.manual_repository import (
    SqlAlchemyManualPlanningTransactionFactory,
)
from app.modules.work.planning.application.manual_service import ManualPlanningService
from app.modules.work.planning.assignment.adapters.recommendation_repository import (
    SqlAlchemyRecommendationTransactionFactory,
)
from app.modules.work.planning.assignment.adapters.repository import (
    SqlAlchemyExplicitAssignmentTransactionFactory,
    SqlAlchemyTeamRequirementTransactionFactory,
)
from app.modules.work.planning.assignment.application.assignment_service import (
    ExplicitTaskAssignmentService,
)
from app.modules.work.planning.assignment.application.recommendation_service import (
    TeamRecommendationService,
)
from app.modules.work.planning.assignment.application.requirement_service import (
    TeamRequirementService,
)

logger = logging.getLogger(__name__)


class _OutboxRunner(Protocol):
    async def dispatch_once(self, worker_id: str, organization_id: UUID) -> bool: ...


class _AssistantRunner(Protocol):
    async def run_once(self, *, worker_id: str, organization_id: UUID) -> bool: ...


class _PlanningRunner(Protocol):
    async def run_once(self, worker_id: str, organization_id: UUID) -> bool: ...


class _ProjectionRunner(Protocol):
    async def project_once(self, *, organization_id: UUID, limit: int = 50) -> int: ...


async def process_tenant_once(
    *,
    worker_id: str,
    organization_id: UUID,
    outbox_service: _OutboxRunner,
    assistant_job_service: _AssistantRunner,
    planning_job_service: _PlanningRunner,
    projection_service: _ProjectionRunner | None = None,
    title_job_service: _AssistantRunner | None = None,
) -> bool:
    """Process bounded Task-8 work in fair fixed order."""
    # Separate job filter and task: naming cannot hold up the current answer.
    title_task = (
        asyncio.create_task(
            title_job_service.run_once(
                worker_id=worker_id,
                organization_id=organization_id,
            )
        )
        if title_job_service is not None
        else None
    )
    processed = False
    try:
        processed = await outbox_service.dispatch_once(worker_id, organization_id) or processed
    except Exception:
        logger.exception("Error processing outbox for organization %s", organization_id)
    try:
        processed = (
            await assistant_job_service.run_once(
                worker_id=worker_id, organization_id=organization_id
            )
            or processed
        )
    except Exception:
        logger.exception("Error processing Assistant jobs for organization %s", organization_id)
    try:
        processed = await planning_job_service.run_once(worker_id, organization_id) or processed
    except Exception:
        logger.exception("Error processing Planning jobs for organization %s", organization_id)
    if projection_service is not None:
        try:
            processed = (
                bool(
                    await projection_service.project_once(
                        organization_id=organization_id,
                        limit=50,
                    )
                )
                or processed
            )
        except Exception:
            logger.exception(
                "Error projecting linked Planning workflows for organization %s",
                organization_id,
            )
    if title_task is not None:
        try:
            processed = await title_task or processed
        except Exception:
            logger.exception("Error processing naming jobs for organization %s", organization_id)
    return processed


async def _run_worker() -> None:
    settings = get_settings()
    worker_id = settings.worker_id
    organization_ids = settings.worker_organization_ids

    if not organization_ids:
        logger.warning(
            "No worker_organization_ids configured. "
            "Worker is idle and will not process any tenants. "
            "Set APP_WORKER_ORGANIZATION_IDS in your environment."
        )

    # Initialize database
    engine = create_async_engine(
        settings.database_url,
        echo=settings.debug,
        pool_size=10,
        max_overflow=20,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    planning_transaction_factory = PostgreSQLPlanningRunTransactionFactory(session_factory)
    assistant_transaction_factory = PostgreSQLAssistantTransactionFactory(session_factory)
    actor_resolver = CurrentActorResolver(
        CurrentActorService(SqlAlchemyAuthTransactionFactory(session_factory))
    )

    scopes = set(organization_ids)

    # Initialize services
    planning_job_service = JobService(
        transaction_factory=planning_transaction_factory,
        handlers=build_planning_job_handlers(
            settings, planning_transaction_factory, actor_resolver
        ),
        organization_scopes=scopes,
        lease_seconds=settings.worker_lease_seconds,
    )
    outbox_service = OutboxService(
        transaction_factory=planning_transaction_factory,
        publisher=UnsupportedOutboxPublisher(),
        organization_scopes=scopes,
        lease_seconds=settings.worker_lease_seconds,
    )
    registry, tool_registry = build_agent_registry()
    planning_runtime = PlanningAIRuntime()
    planning_run_service = PlanningRunService(
        transaction_factory=planning_transaction_factory,
        runtime=planning_runtime,
    )
    proposal_service = ProposalService(
        transaction_factory=planning_transaction_factory,
        runtime=planning_runtime,
    )
    task_service = TaskService(SqlAlchemyTaskTransactionFactory(session_factory))
    member_service = MemberService(SqlAlchemyMemberTransactionFactory(session_factory))
    manual_planning_service = ManualPlanningService(
        SqlAlchemyManualPlanningTransactionFactory(session_factory)
    )
    project_service = ProjectService(SqlAlchemyProjectTransactionFactory(session_factory))
    people_capacity_service = PeopleCapacityService(
        SqlAlchemyPeopleCapacityTransactionFactory(session_factory)
    )
    requirement_service = TeamRequirementService(
        SqlAlchemyTeamRequirementTransactionFactory(session_factory)
    )
    recommendation_service = TeamRecommendationService(
        SqlAlchemyRecommendationTransactionFactory(session_factory)
    )
    explicit_assignment_service = ExplicitTaskAssignmentService(
        SqlAlchemyExplicitAssignmentTransactionFactory(session_factory)
    )
    work_tool_backend = WorkToolExecutor(
        actor_resolver=actor_resolver,
        tool_registry=tool_registry,
        task_service=task_service,
        project_service=project_service,
        planning_service=manual_planning_service,
    )
    work_tool_executor = RecordingToolExecutor(
        transaction_factory=assistant_transaction_factory,
        tool_registry=tool_registry,
        backend=work_tool_backend,
    )
    planning_tool_executor = RecordingToolExecutor(
        transaction_factory=assistant_transaction_factory,
        tool_registry=tool_registry,
        backend=AssistantPlanningToolAdapter(
            actor_resolver=actor_resolver,
            assistant_transaction_factory=assistant_transaction_factory,
            planning_run_service=planning_run_service,
            proposal_service=proposal_service,
        ),
    )
    assignment_application = AssignmentApplicationService(
        actor_resolver=actor_resolver,
        requirements=requirement_service,
        recommendations=recommendation_service,
        assignments=explicit_assignment_service,
        people_capacity=people_capacity_service,
        planning=manual_planning_service,
        members=member_service,
    )
    assignment_tool_executor = RecordingToolExecutor(
        transaction_factory=assistant_transaction_factory,
        tool_registry=tool_registry,
        backend=AssistantAssignmentToolAdapter(application=assignment_application),
    )
    model_gateway = build_model_gateway(settings)
    daily_updates, daily_assessments, daily_usage = build_daily_services(
        sessions=session_factory,
        settings=settings,
        gateway=AgentRecordingModelGateway(
            gateway=model_gateway, transaction_factory=assistant_transaction_factory
        ),
        evidence=EvidenceService(
            SqlAlchemyEvidenceTransactionFactory(session_factory),
            FilesystemEvidenceStorage(settings.evidence_storage_root),
        ),
    )
    daily_tools = RecordingToolExecutor(
        transaction_factory=assistant_transaction_factory,
        tool_registry=tool_registry,
        backend=DailyUpdateToolAdapter(
            actors=actor_resolver, updates=daily_updates, assessments=daily_assessments
        ),
    )
    turn_executor = AssistantTurnExecutor(
        transaction_factory=assistant_transaction_factory,
        registry=registry,
        engine_factory=build_execution_engine_factory(
            model_gateway=model_gateway,
            registry=registry,
            actor_resolver=actor_resolver,
            work_tool_executor=work_tool_executor,
            transaction_factory=assistant_transaction_factory,
            planning_tool_executor=planning_tool_executor,
            assignment_tool_executor=assignment_tool_executor,
            daily_update_tool_executor=daily_tools,
            daily_usage_store=daily_usage,
            daily_image_token_bound=image_token_bound(settings),
        ),
        daily_update_context_resolver=DailyUpdateContextResolver(
            tasks=task_service, updates=daily_updates
        ),
        assignment_context_resolver=AssistantAssignmentContextResolver(
            tasks=task_service,
            members=member_service,
            projects=project_service,
        ),
    )
    assistant_execution_service = AssistantExecutionService(
        actor_resolver=actor_resolver,
        runtime=AssistantAgentRuntime(turn_executor),
    )
    assistant_job_service = AssistantJobService(
        transaction_factory=assistant_transaction_factory,
        handler=assistant_execution_service.execute,
        organization_scopes=scopes,
        lease_seconds=settings.worker_lease_seconds,
    )
    title_execution_service = AssistantExecutionService(
        actor_resolver=actor_resolver,
        runtime=ConversationTitleService(
            transaction_factory=assistant_transaction_factory,
            gateway=build_title_gateway(settings),
            timeout_seconds=settings.ai_title_timeout_seconds,
        ),
    )
    title_job_service = AssistantJobService(
        transaction_factory=assistant_transaction_factory,
        handler=title_execution_service.execute,
        organization_scopes=scopes,
        lease_seconds=settings.worker_lease_seconds,
        job_type="assistant.conversation.title",
    )
    projection_service = AssistantProjectionService(
        transaction_factory=assistant_transaction_factory
    )

    logger.info("Worker %s started", worker_id)

    poll_interval = settings.worker_poll_interval_seconds
    running = True

    def _handle_shutdown() -> None:
        logger.info("Worker shutting down...")
        nonlocal running
        running = False

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_shutdown)

    while running:
        if not organization_ids:
            await asyncio.sleep(poll_interval)
            continue

        processed_any = False

        for org_id in organization_ids:
            if not running:
                break

            processed_any = (
                await process_tenant_once(
                    worker_id=worker_id,
                    organization_id=org_id,
                    outbox_service=outbox_service,
                    assistant_job_service=assistant_job_service,
                    planning_job_service=planning_job_service,
                    projection_service=projection_service,
                    title_job_service=title_job_service,
                )
                or processed_any
            )

        if not processed_any and running:
            await asyncio.sleep(poll_interval)

    logger.info("Worker %s stopped", worker_id)


def main() -> NoReturn:
    """Synchronous entrypoint for the worker script."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run_worker())

    sys.exit(0)


if __name__ == "__main__":
    main()
