"""Manager-only schedule preview, confirmation, read and pause/resume."""

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

from ..application.schedule_service import ScheduleService, ScheduleView
from ..domain.schedules import (
    ConfirmSchedule,
    DailySummarySchedule,
    PauseSchedule,
    ScheduleDraft,
    ScheduleError,
)
from .dependencies import get_schedule_service
from .schemas import SchedulePreviewRequest


class ScheduleRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def preflight(request: Request) -> Response:
            if request.method in {"POST", "DELETE"}:
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
                service = get_schedule_service(request)
                request.state.mutation_rejection_audit = partial(
                    service.audit_transport_rejection,
                    actor=actor,
                    key=request.headers.get("Idempotency-Key", ""),
                    request_id=str(request.state.request_id),
                )
            return await handler(request)

        return preflight


router = APIRouter(
    prefix="/automations/daily-summaries", tags=["automations"], route_class=ScheduleRoute
)
Service = Annotated[ScheduleService, Depends(get_schedule_service)]
Key = Annotated[str | None, Header(alias="Idempotency-Key")]
_ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorResponse} for status in (400, 401, 403, 404, 409, 422)
}


def error(exc: ScheduleError) -> ApplicationError:
    return ApplicationError(
        status_code=exc.status, code=exc.code, message_key=f"automations.error.{exc.code}"
    )


@router.get("", response_model=ScheduleView, responses=_ERRORS)
async def get(project_id: UUID, actor: ActorDependency, service: Service) -> ScheduleView:
    try:
        return await service.get(actor, project_id)
    except ScheduleError as exc:
        raise error(exc) from exc


@router.post("/preview", response_model=ScheduleDraft, status_code=201, responses=_ERRORS)
async def preview(
    body: SchedulePreviewRequest, actor: ActorDependency, service: Service, key: Key = None
) -> ScheduleDraft:
    try:
        return await service.preview(actor, body.command, body.expected_version, key or "")
    except ScheduleError as exc:
        raise error(exc) from exc


@router.post("/confirm", response_model=DailySummarySchedule, status_code=201, responses=_ERRORS)
async def confirm(
    body: ConfirmSchedule, actor: ActorDependency, service: Service, key: Key = None
) -> DailySummarySchedule:
    try:
        return await service.confirm(actor, body.draft_id, body.expected_version, key or "")
    except ScheduleError as exc:
        raise error(exc) from exc


@router.post("/{schedule_id}/pause", response_model=DailySummarySchedule, responses=_ERRORS)
async def pause(
    schedule_id: UUID,
    body: PauseSchedule,
    actor: ActorDependency,
    service: Service,
    key: Key = None,
) -> DailySummarySchedule:
    try:
        return await service.pause(
            actor, schedule_id, body.expected_version, body.paused, key or ""
        )
    except ScheduleError as exc:
        raise error(exc) from exc
