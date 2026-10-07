"""Advisory feedback accepts no approval, origin, role or model identifiers."""

from collections.abc import Callable, Coroutine
from functools import partial
from inspect import isawaitable
from typing import Any, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.routing import APIRoute

from app.api.errors import ApplicationError, ErrorResponse
from app.core.config import Settings
from app.modules.identity.api.dependencies import ActorDependency, get_authenticated_actor
from app.modules.identity.application.auth_service import AuthService
from app.modules.reporting.api.routes import IdempotencyHeader
from app.modules.reporting.domain.reports import ReportError

from ..domain.feedback import FeedbackCommand
from ..domain.outcomes import ReviewRates
from .dependencies import (
    FeedbackServiceDependency,
    OutcomeServiceDependency,
    get_feedback_service,
    get_outcome_service,
)
from .schemas import FeedbackRequest, FeedbackResponse, OutcomeRequest, OutcomeResponse

FEEDBACK_ERRORS: dict[int | str, dict[str, Any]] = {
    n: {"model": ErrorResponse} for n in (400, 401, 403, 404, 409, 412, 422)
}


def raise_feedback_error(error: ReportError) -> NoReturn:
    message = {
        403: "forbidden",
        404: "notFound",
        409: "idempotencyKeyReused",
        422: "validation",
    }.get(error.status, "invalidRequest")
    raise ApplicationError(
        status_code=error.status, code=error.code, message_key=f"common.error.{message}"
    ) from error


class FeedbackRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def preflight(request: Request) -> Response:
            if request.method != "POST":
                return await handler(request)
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
            if request.url.path.endswith("/outcomes"):
                try:
                    feedback_id = UUID(str(request.path_params.get("feedback_id")))
                except ValueError:
                    feedback_id = None
                request.state.mutation_rejection_audit = partial(
                    get_outcome_service(request).audit_rejection,
                    actor=actor,
                    feedback_id=feedback_id,
                    key=request.headers.get("Idempotency-Key"),
                )
                return await handler(request)
            # Extract only opaque IDs for rejection audit; all other body fields remain untrusted.
            command: FeedbackCommand | None = None
            try:
                body = await request.json()
                command = FeedbackCommand(
                    report_id=UUID(str(body.get("report_id"))),
                    report_version_id=UUID(str(body.get("report_version_id"))),
                    decision="REJECT",
                    reason="Invalid request",
                )
            except (ValueError, TypeError, AttributeError):
                pass
            request.state.mutation_rejection_audit = partial(
                get_feedback_service(request).audit_rejection,
                actor=actor,
                command=command,
                key=request.headers.get("Idempotency-Key"),
            )
            return await handler(request)

        return preflight


router = APIRouter(prefix="/feedback", tags=["feedback"], route_class=FeedbackRoute)


@router.post("", response_model=FeedbackResponse, status_code=201, responses=FEEDBACK_ERRORS)
async def record_feedback(
    body: FeedbackRequest,
    actor: ActorDependency,
    service: FeedbackServiceDependency,
    key: IdempotencyHeader,
    request: Request,
    response: Response,
) -> FeedbackResponse:
    request.state.mutation_rejection_audit = None
    try:
        result = await service.record(actor=actor, command=body, idempotency_key=key)
    except ReportError as exc:
        raise_feedback_error(exc)
    response.headers["Cache-Control"] = "private, no-store"
    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return FeedbackResponse.model_validate(result.model_dump())


@router.post(
    "/{feedback_id}/outcomes",
    response_model=OutcomeResponse,
    status_code=201,
    responses=FEEDBACK_ERRORS,
)
async def record_outcome(
    feedback_id: UUID,
    body: OutcomeRequest,
    actor: ActorDependency,
    service: OutcomeServiceDependency,
    key: IdempotencyHeader,
    request: Request,
    response: Response,
) -> OutcomeResponse:
    request.state.mutation_rejection_audit = None
    try:
        result = await service.record(
            actor=actor, feedback_id=feedback_id, source=body, idempotency_key=key
        )
    except ReportError as exc:
        raise_feedback_error(exc)
    response.headers["Cache-Control"] = "private, no-store"
    return OutcomeResponse.model_validate(result.model_dump())


@router.get("/rates", response_model=ReviewRates, responses=FEEDBACK_ERRORS)
async def rates(
    project_id: UUID, actor: ActorDependency, service: FeedbackServiceDependency, response: Response
) -> ReviewRates:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await service.rates(actor=actor, project_id=project_id)
    except ReportError as exc:
        raise_feedback_error(exc)
