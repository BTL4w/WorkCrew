"""FastAPI application factory and ASGI entrypoint."""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.errors import register_error_handlers
from app.core.config import Settings, get_settings
from app.core.database import create_database_engine, create_session_factory
from app.modules.assistant.adapters.assignment_tools import TeamRecommendationSnapshotAdapter
from app.modules.assistant.adapters.planning_snapshot import PostgreSQLPlanningSnapshot
from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory
from app.modules.assistant.api.routes import router as assistant_router
from app.modules.assistant.application.event_service import AssistantEventService
from app.modules.assistant.application.service import AssistantService
from app.modules.identity.adapters.runtime import create_auth_runtime
from app.modules.identity.api.routes import router as auth_router
from app.modules.identity.application.auth_service import AuthService
from app.modules.organization.adapters.member_repository import SqlAlchemyMemberTransactionFactory
from app.modules.organization.api.members import router as member_router
from app.modules.organization.application.member_service import MemberService
from app.modules.people_capacity.adapters.repository import (
    SqlAlchemyPeopleCapacityTransactionFactory,
)
from app.modules.people_capacity.api.routes import router as people_capacity_router
from app.modules.people_capacity.application.service import PeopleCapacityService
from app.modules.planning_runs.adapters.ai_runtime import PlanningAIRuntime
from app.modules.planning_runs.adapters.transaction import (
    PostgreSQLPlanningRunTransactionFactory,
)
from app.modules.planning_runs.api.routes import router as planning_run_router
from app.modules.planning_runs.application.approval_service import ApprovalService
from app.modules.planning_runs.application.event_service import WorkflowEventService
from app.modules.planning_runs.application.proposal_service import ProposalService
from app.modules.planning_runs.application.run_service import PlanningRunService
from app.modules.progress.adapters.daily_update_repository import SqlAlchemyDailyUpdateTransactions
from app.modules.progress.adapters.evidence_repository import SqlAlchemyEvidenceTransactionFactory
from app.modules.progress.adapters.filesystem_storage import FilesystemEvidenceStorage
from app.modules.progress.adapters.progress_repository import SqlAlchemyProgressTransactionFactory
from app.modules.progress.api.daily_update_routes import router as daily_update_router
from app.modules.progress.api.evidence_routes import router as evidence_router
from app.modules.progress.api.progress_routes import router as progress_router
from app.modules.progress.application.daily_update_service import DailyUpdateService
from app.modules.progress.application.evidence_service import EvidenceService
from app.modules.progress.application.weekly_progress_service import WeeklyProgressService
from app.modules.work.adapters.project_repository import SqlAlchemyProjectTransactionFactory
from app.modules.work.adapters.task_repository import SqlAlchemyTaskTransactionFactory
from app.modules.work.api.routes import router as project_router
from app.modules.work.api.task_routes import router as task_router
from app.modules.work.application.project_service import ProjectService
from app.modules.work.application.task_service import TaskService
from app.modules.work.planning.adapters.manual_repository import (
    SqlAlchemyManualPlanningTransactionFactory,
)
from app.modules.work.planning.api.routes import router as planning_router
from app.modules.work.planning.application.manual_service import ManualPlanningService
from app.modules.work.planning.assignment.adapters.recommendation_repository import (
    SqlAlchemyRecommendationTransactionFactory,
)
from app.modules.work.planning.assignment.adapters.repository import (
    SqlAlchemyExplicitAssignmentTransactionFactory,
    SqlAlchemyTeamRequirementTransactionFactory,
)
from app.modules.work.planning.assignment.api.recommendation_routes import (
    router as team_recommendation_router,
)
from app.modules.work.planning.assignment.api.routes import router as team_requirement_router
from app.modules.work.planning.assignment.application.assignment_service import (
    ExplicitTaskAssignmentService,
)
from app.modules.work.planning.assignment.application.recommendation_service import (
    TeamRecommendationService,
)
from app.modules.work.planning.assignment.application.requirement_service import (
    TeamRequirementService,
)
from work_management_ai.runtime.manifests import (
    AgentManifest,
    canonical_manifest_fingerprint,
    load_yaml_resource,
)

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _resolve_request_id(request: Request) -> str:
    candidate = request.headers.get("X-Request-ID")
    if candidate is not None and _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return uuid4().hex


def create_app(
    settings: Settings | None = None,
    auth_service: AuthService | None = None,
    project_service: ProjectService | None = None,
    task_service: TaskService | None = None,
    member_service: MemberService | None = None,
    manual_planning_service: ManualPlanningService | None = None,
    planning_run_service: PlanningRunService | None = None,
    proposal_service: ProposalService | None = None,
    workflow_event_service: WorkflowEventService | None = None,
    approval_service: ApprovalService | None = None,
    assistant_service: AssistantService | None = None,
    assistant_event_service: AssistantEventService | None = None,
    people_capacity_service: PeopleCapacityService | None = None,
    team_requirement_service: TeamRequirementService | None = None,
    team_recommendation_service: TeamRecommendationService | None = None,
    explicit_assignment_service: ExplicitTaskAssignmentService | None = None,
    evidence_service: EvidenceService | None = None,
) -> FastAPI:
    """Build an isolated application instance for runtime or tests."""

    resolved_settings = settings or get_settings()
    database_engine: AsyncEngine | None = None
    resolved_auth_service = auth_service
    if resolved_auth_service is None:
        resolved_auth_service, database_engine = create_auth_runtime(resolved_settings)
    resolved_project_service = project_service
    if resolved_project_service is None:
        if database_engine is None:
            database_engine = create_database_engine(resolved_settings)
        resolved_project_service = ProjectService(
            SqlAlchemyProjectTransactionFactory(create_session_factory(database_engine))
        )
    resolved_task_service = task_service
    if resolved_task_service is None:
        if database_engine is None:
            database_engine = create_database_engine(resolved_settings)
        resolved_task_service = TaskService(
            SqlAlchemyTaskTransactionFactory(create_session_factory(database_engine))
        )
    resolved_member_service = member_service
    if resolved_member_service is None:
        if database_engine is None:
            database_engine = create_database_engine(resolved_settings)
        resolved_member_service = MemberService(
            SqlAlchemyMemberTransactionFactory(create_session_factory(database_engine))
        )
    resolved_people_capacity_service = people_capacity_service
    if resolved_people_capacity_service is None:
        if database_engine is None:
            database_engine = create_database_engine(resolved_settings)
        resolved_people_capacity_service = PeopleCapacityService(
            SqlAlchemyPeopleCapacityTransactionFactory(create_session_factory(database_engine))
        )
    resolved_team_requirement_service = team_requirement_service
    if resolved_team_requirement_service is None:
        if database_engine is None:
            database_engine = create_database_engine(resolved_settings)
        resolved_team_requirement_service = TeamRequirementService(
            SqlAlchemyTeamRequirementTransactionFactory(create_session_factory(database_engine))
        )
    resolved_explicit_assignment_service = explicit_assignment_service
    if resolved_explicit_assignment_service is None:
        if database_engine is None:
            database_engine = create_database_engine(resolved_settings)
        resolved_explicit_assignment_service = ExplicitTaskAssignmentService(
            SqlAlchemyExplicitAssignmentTransactionFactory(create_session_factory(database_engine))
        )
    resolved_manual_planning_service = manual_planning_service
    resolved_team_recommendation_service = team_recommendation_service
    if resolved_team_recommendation_service is None:
        if database_engine is None:
            database_engine = create_database_engine(resolved_settings)
        resolved_team_recommendation_service = TeamRecommendationService(
            SqlAlchemyRecommendationTransactionFactory(create_session_factory(database_engine))
        )
    if resolved_manual_planning_service is None:
        if database_engine is None:
            database_engine = create_database_engine(resolved_settings)
        resolved_manual_planning_service = ManualPlanningService(
            SqlAlchemyManualPlanningTransactionFactory(create_session_factory(database_engine))
        )
    if evidence_service is None:
        if database_engine is None:
            database_engine = create_database_engine(resolved_settings)
        evidence_service = EvidenceService(
            SqlAlchemyEvidenceTransactionFactory(create_session_factory(database_engine)),
            FilesystemEvidenceStorage(resolved_settings.evidence_storage_root),
        )
    runtime = PlanningAIRuntime()
    resolved_planning_run_service = planning_run_service
    resolved_proposal_service = proposal_service
    resolved_workflow_event_service = workflow_event_service
    resolved_approval_service = approval_service
    if (
        resolved_planning_run_service is None
        or resolved_proposal_service is None
        or resolved_workflow_event_service is None
        or resolved_approval_service is None
    ):
        if database_engine is None:
            database_engine = create_database_engine(resolved_settings)
        planning_transaction_factory = PostgreSQLPlanningRunTransactionFactory(
            create_session_factory(database_engine)
        )
        if resolved_planning_run_service is None:
            resolved_planning_run_service = PlanningRunService(
                transaction_factory=planning_transaction_factory,
                runtime=runtime,
            )
        if resolved_proposal_service is None:
            resolved_proposal_service = ProposalService(
                transaction_factory=planning_transaction_factory,
                runtime=runtime,
            )
        if resolved_workflow_event_service is None:
            resolved_workflow_event_service = WorkflowEventService(
                transaction_factory=planning_transaction_factory
            )
        if resolved_approval_service is None:
            resolved_approval_service = ApprovalService(
                transaction_factory=planning_transaction_factory,
                runtime=runtime,
            )

    resolved_assistant_service = assistant_service
    resolved_assistant_event_service = assistant_event_service
    if resolved_assistant_service is None or resolved_assistant_event_service is None:
        if database_engine is None:
            database_engine = create_database_engine(resolved_settings)
        assistant_transaction_factory = PostgreSQLAssistantTransactionFactory(
            create_session_factory(database_engine)
        )
        if resolved_assistant_service is None:
            orchestrator_manifest = load_yaml_resource(
                "work_management_ai.agents.orchestrator", "agent.yaml", AgentManifest
            )
            resolved_assistant_service = AssistantService(
                transaction_factory=assistant_transaction_factory,
                planning_snapshot=PostgreSQLPlanningSnapshot(
                    PostgreSQLPlanningRunTransactionFactory(create_session_factory(database_engine))
                ),
                team_recommendation_snapshot=TeamRecommendationSnapshotAdapter(
                    resolved_team_recommendation_service
                ),
                orchestrator_version=orchestrator_manifest.agent.version,
                orchestrator_fingerprint=canonical_manifest_fingerprint(orchestrator_manifest),
            )
        if resolved_assistant_event_service is None:
            resolved_assistant_event_service = AssistantEventService(
                transaction_factory=assistant_transaction_factory,
            )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            if database_engine is not None:
                await database_engine.dispose()

    app = FastAPI(
        title=resolved_settings.name,
        version=resolved_settings.version,
        debug=resolved_settings.debug,
        lifespan=lifespan,
    )
    app.state.evidence_service = evidence_service
    if database_engine is None:
        database_engine = create_database_engine(resolved_settings)
    app.state.weekly_progress_service = WeeklyProgressService(
        SqlAlchemyProgressTransactionFactory(create_session_factory(database_engine))
    )
    app.state.daily_update_service = DailyUpdateService(
        SqlAlchemyDailyUpdateTransactions(create_session_factory(database_engine)),
        resolved_settings.reporting_timezone,
    )
    app.state.settings = resolved_settings
    app.state.auth_service = resolved_auth_service
    app.state.project_service = resolved_project_service
    app.state.task_service = resolved_task_service
    app.state.member_service = resolved_member_service
    app.state.people_capacity_service = resolved_people_capacity_service
    app.state.team_requirement_service = resolved_team_requirement_service
    app.state.team_recommendation_service = resolved_team_recommendation_service
    app.state.explicit_assignment_service = resolved_explicit_assignment_service
    app.state.manual_planning_service = resolved_manual_planning_service
    app.state.planning_run_service = resolved_planning_run_service
    app.state.proposal_service = resolved_proposal_service
    app.state.workflow_event_service = resolved_workflow_event_service
    app.state.approval_service = resolved_approval_service
    app.state.assistant_service = resolved_assistant_service
    app.state.assistant_event_service = resolved_assistant_event_service
    app.state.database_engine = database_engine
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[resolved_settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "X-Request-ID",
            "Idempotency-Key",
            "If-Match",
            "X-Evidence-Filename",
        ],
    )

    @app.middleware("http")
    async def request_context(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = _resolve_request_id(request)
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    register_error_handlers(app)
    app.include_router(evidence_router, prefix="/api/v1")
    app.include_router(progress_router, prefix="/api/v1")
    app.include_router(daily_update_router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(project_router, prefix="/api/v1")
    app.include_router(task_router, prefix="/api/v1")
    app.include_router(member_router, prefix="/api/v1")
    app.include_router(people_capacity_router, prefix="/api/v1")
    app.include_router(team_requirement_router, prefix="/api/v1")
    app.include_router(team_recommendation_router, prefix="/api/v1")
    app.include_router(planning_router, prefix="/api/v1")
    app.include_router(planning_run_router, prefix="/api/v1")
    app.include_router(assistant_router, prefix="/api/v1")
    return app


app = create_app()
