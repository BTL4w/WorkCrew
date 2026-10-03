"""Read-only AI risk analysis and direct authorized Manager review."""

from collections.abc import Callable, Coroutine
from functools import partial
from inspect import isawaitable
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import Response
from fastapi.routing import APIRoute

from app.api.errors import ApplicationError, ErrorResponse
from app.core.config import Settings
from app.modules.identity.api.dependencies import ActorDependency, get_authenticated_actor
from app.modules.identity.application.auth_service import AuthService
from app.modules.progress.domain.blockers import BlockerError
from app.modules.risk.api.dependencies import get_risk_service
from app.modules.risk.api.schemas import RiskRefreshRequest
from app.modules.risk.application.notification_service import NotificationService
from app.modules.risk.application.review_service import RiskReviewService
from app.modules.risk.application.risk_service import RiskService
from app.modules.risk.domain.assessments import (
    RiskAssessment,
    RiskReviewCommand,
    RiskReviewEvent,
    WeeklyRisk,
)
from app.modules.risk.domain.notifications import RiskNotification

Service = Annotated[RiskService, Depends(get_risk_service)]
Key = Annotated[str | None, Header(alias="Idempotency-Key")]


class RiskRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def preflight(request: Request) -> Response:
            if request.method in {"POST", "PATCH", "DELETE"}:
                override = request.app.dependency_overrides.get(get_authenticated_actor)
                if override is not None:
                    candidate = override()
                    actor = await candidate if isawaitable(candidate) else candidate
                else:
                    actor = await get_authenticated_actor(
                        request,
                        cast(AuthService, request.app.state.auth_service),
                        cast(Settings, request.app.state.settings),
                    )
                service = get_risk_service(request)
                key = request.headers.get("Idempotency-Key")
                request.state.mutation_rejection_audit = partial(
                    service.audit_transport_rejection,
                    actor=actor,
                    request_id=str(request.state.request_id),
                    key=key,
                )
            return await handler(request)

        return preflight


router = APIRouter(tags=["risk"], route_class=RiskRoute)
_ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (400, 401, 403, 404, 409, 422)
}


def error(exc: BlockerError) -> ApplicationError:
    return ApplicationError(
        status_code=exc.status, code=exc.code, message_key=f"risk.error.{exc.code}"
    )


@router.post("/risks/refresh", response_model=RiskAssessment, status_code=202, responses=_ERRORS)
async def refresh(
    body: RiskRefreshRequest, actor: ActorDependency, service: Service, idempotency_key: Key = None
) -> RiskAssessment:
    try:
        return await service.request_refresh(actor, body.task_id, idempotency_key or "")
    except BlockerError as exc:
        raise error(exc) from exc


@router.get("/risks", response_model=RiskAssessment | None, responses=_ERRORS)
async def current(task_id: UUID, actor: ActorDependency, service: Service) -> RiskAssessment | None:
    try:
        return await service.current(actor, task_id)
    except BlockerError as exc:
        raise error(exc) from exc


@router.get("/risks/weeks/{week_id}", response_model=WeeklyRisk, responses=_ERRORS)
async def weekly(week_id: UUID, actor: ActorDependency, service: Service) -> WeeklyRisk:
    try:
        async with service.transactions(actor) as repo:
            await repo.authenticate()
            return await repo.weekly(week_id)
    except BlockerError as exc:
        raise error(exc) from exc


@router.get("/risks/{risk_id}", response_model=RiskAssessment, responses=_ERRORS)
async def get_risk(risk_id: UUID, actor: ActorDependency, service: Service) -> RiskAssessment:
    try:
        async with service.transactions(actor) as repo:
            await repo.authenticate()
            return await repo.get(risk_id)
    except BlockerError as exc:
        raise error(exc) from exc


@router.post(
    "/risks/{risk_id}/reviews", response_model=RiskReviewEvent, status_code=201, responses=_ERRORS
)
async def review(
    risk_id: UUID,
    body: RiskReviewCommand,
    actor: ActorDependency,
    service: Service,
    idempotency_key: Key = None,
) -> RiskReviewEvent:
    try:
        return await RiskReviewService(service.transactions).record(
            actor, risk_id, body, idempotency_key or ""
        )
    except BlockerError as exc:
        raise error(exc) from exc


@router.get(
    "/risks/{risk_id}/reviews", response_model=tuple[RiskReviewEvent, ...], responses=_ERRORS
)
async def reviews(
    risk_id: UUID, actor: ActorDependency, service: Service
) -> tuple[RiskReviewEvent, ...]:
    try:
        return await RiskReviewService(service.transactions).list(actor, risk_id)
    except BlockerError as exc:
        raise error(exc) from exc


@router.get("/notifications", response_model=tuple[RiskNotification, ...], responses=_ERRORS)
async def notifications(actor: ActorDependency, service: Service) -> tuple[RiskNotification, ...]:
    try:
        return await NotificationService(service.transactions).list(actor)
    except BlockerError as exc:
        raise error(exc) from exc


@router.post(
    "/notifications/{notification_id}/read", response_model=RiskNotification, responses=_ERRORS
)
async def read_notification(
    notification_id: UUID, actor: ActorDependency, service: Service, idempotency_key: Key = None
) -> RiskNotification:
    try:
        return await NotificationService(service.transactions).mark_read(
            actor, notification_id, idempotency_key or ""
        )
    except BlockerError as exc:
        raise error(exc) from exc
