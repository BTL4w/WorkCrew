"""Explicit Admin evaluation start and metadata-only status."""

from functools import partial
from inspect import isawaitable
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.routing import APIRoute

from app.core.config import Settings
from app.modules.identity.api.dependencies import ActorDependency, get_authenticated_actor
from app.modules.identity.application.auth_service import AuthService
from app.modules.reporting.api.routes import IdempotencyHeader
from app.modules.reporting.domain.reports import ReportError

from ..application.evaluation_service import EvaluationService
from ..domain.evaluation_runs import EvaluationRequest, EvaluationRun
from .routes import FEEDBACK_ERRORS, raise_feedback_error


def get_evaluation_service(request: Request) -> EvaluationService:
    return cast(EvaluationService, request.app.state.evaluation_service)


class EvaluationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def preflight(request: Request) -> Response:
            if request.method == "POST":
                override = request.app.dependency_overrides.get(get_authenticated_actor)
                if override:
                    candidate = override()
                    actor = await candidate if isawaitable(candidate) else candidate
                else:
                    actor = await get_authenticated_actor(
                        request,
                        cast(AuthService, request.app.state.auth_service),
                        cast(Settings, request.app.state.settings),
                    )
                request.state.mutation_rejection_audit = partial(
                    get_evaluation_service(request).audit_rejection,
                    actor=actor,
                    key=request.headers.get("Idempotency-Key"),
                )
            return await handler(request)

        return preflight


router = APIRouter(prefix="/evaluations/runs", tags=["evaluations"], route_class=EvaluationRoute)
Service = Annotated[EvaluationService, Depends(get_evaluation_service)]


@router.post("", response_model=EvaluationRun, status_code=202, responses=FEEDBACK_ERRORS)
async def start(
    body: EvaluationRequest,
    actor: ActorDependency,
    service: Service,
    key: IdempotencyHeader,
    request: Request,
    response: Response,
) -> EvaluationRun:
    request.state.mutation_rejection_audit = None
    try:
        result = await service.start(actor=actor, request=body, idempotency_key=key)
    except ReportError as exc:
        raise_feedback_error(exc)
    response.headers["Cache-Control"] = "private, no-store"
    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return result


@router.get("/{run_id}", response_model=EvaluationRun, responses=FEEDBACK_ERRORS)
async def get(
    run_id: UUID, actor: ActorDependency, service: Service, response: Response
) -> EvaluationRun:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await service.get(actor=actor, run_id=run_id)
    except ReportError as exc:
        raise_feedback_error(exc)
