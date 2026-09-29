"""Manual submit and confirmation share the same service and immutable draft."""

from collections.abc import Callable, Coroutine
from functools import partial
from inspect import isawaitable
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response
from fastapi.routing import APIRoute

from app.api.errors import ApplicationError, ErrorResponse
from app.core.config import Settings
from app.modules.identity.api.dependencies import ActorDependency, get_authenticated_actor
from app.modules.identity.application.auth_service import AuthService
from app.modules.progress.api.daily_update_schemas import (
    DailyUpdateDraftRequest,
    DailyUpdateRevisionRequest,
)
from app.modules.progress.api.dependencies import (
    DailyUpdateServiceDependency,
    get_daily_update_service,
)
from app.modules.progress.domain.daily_updates import (
    ConfirmDailyUpdateCommand,
    ConfirmedDailyUpdate,
    ConfirmedObservation,
    DailyUpdateDraft,
    DailyUpdateError,
    TaskReportingContext,
)


class DailyUpdateRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def preflight(request: Request) -> Response:
            if request.method in {"POST", "PATCH"}:
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
                service = get_daily_update_service(request)
                key = request.headers.get("Idempotency-Key")
                request.state.mutation_rejection_audit = partial(
                    service.audit_transport_rejection,
                    actor=actor,
                    request_id=str(request.state.request_id),
                    key=key,
                )
            return await handler(request)

        return preflight


router = APIRouter(tags=["daily-updates"], route_class=DailyUpdateRoute)
_ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (400, 401, 403, 404, 409, 422)
}
Key = Annotated[str | None, Header(alias="Idempotency-Key")]


def error(exc: DailyUpdateError) -> ApplicationError:
    return ApplicationError(
        status_code=exc.status, code=exc.code, message_key=f"dailyUpdate.error.{exc.code}"
    )


@router.post(
    "/daily-updates/drafts", response_model=DailyUpdateDraft, status_code=201, responses=_ERRORS
)
async def create_draft(
    body: DailyUpdateDraftRequest,
    actor: ActorDependency,
    service: DailyUpdateServiceDependency,
    request: Request,
    response: Response,
    key: Key = None,
) -> DailyUpdateDraft:
    response.headers["Cache-Control"] = "private, no-store"
    request.state.mutation_rejection_audit = None
    try:
        return await service.create_draft(
            actor, body.items, key or "", str(request.state.request_id)
        )
    except DailyUpdateError as exc:
        raise error(exc) from exc


@router.get("/daily-updates/drafts/{draft_id}", response_model=DailyUpdateDraft, responses=_ERRORS)
async def get_draft(
    draft_id: UUID,
    actor: ActorDependency,
    service: DailyUpdateServiceDependency,
    response: Response,
) -> DailyUpdateDraft:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await service.get_draft(actor, draft_id)
    except DailyUpdateError as exc:
        raise error(exc) from exc


@router.patch("/daily-updates/{draft_id}/draft", response_model=DailyUpdateDraft, responses=_ERRORS)
async def revise_draft(
    draft_id: UUID,
    body: DailyUpdateRevisionRequest,
    actor: ActorDependency,
    service: DailyUpdateServiceDependency,
    request: Request,
    response: Response,
    key: Key = None,
) -> DailyUpdateDraft:
    response.headers["Cache-Control"] = "private, no-store"
    request.state.mutation_rejection_audit = None
    try:
        return await service.revise_draft(
            actor,
            draft_id,
            body.expected_version,
            body.items,
            key or "",
            str(request.state.request_id),
        )
    except DailyUpdateError as exc:
        raise error(exc) from exc


@router.post(
    "/daily-updates/{draft_id}/confirm",
    response_model=ConfirmedDailyUpdate,
    status_code=201,
    responses=_ERRORS,
)
async def confirm_update(
    draft_id: UUID,
    body: ConfirmDailyUpdateCommand,
    actor: ActorDependency,
    service: DailyUpdateServiceDependency,
    request: Request,
    response: Response,
    key: Key = None,
) -> ConfirmedDailyUpdate:
    response.headers["Cache-Control"] = "private, no-store"
    request.state.mutation_rejection_audit = None
    if draft_id != body.draft_id:
        await service.reject(actor, str(request.state.request_id), key, "INVALID_DRAFT", draft_id)
        raise error(DailyUpdateError("INVALID_DRAFT", 422))
    try:
        return await service.confirm(actor, body, key or "", str(request.state.request_id))
    except DailyUpdateError as exc:
        raise error(exc) from exc


@router.post(
    "/daily-updates", response_model=ConfirmedDailyUpdate, status_code=201, responses=_ERRORS
)
async def manual_submit(
    body: ConfirmDailyUpdateCommand,
    actor: ActorDependency,
    service: DailyUpdateServiceDependency,
    request: Request,
    response: Response,
    key: Key = None,
) -> ConfirmedDailyUpdate:
    response.headers["Cache-Control"] = "private, no-store"
    request.state.mutation_rejection_audit = None
    try:
        return await service.confirm(actor, body, key or "", str(request.state.request_id))
    except DailyUpdateError as exc:
        raise error(exc) from exc


@router.get("/daily-updates", response_model=tuple[ConfirmedObservation, ...], responses=_ERRORS)
async def history(
    task_id: UUID, actor: ActorDependency, service: DailyUpdateServiceDependency, response: Response
) -> tuple[ConfirmedObservation, ...]:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await service.history(actor, task_id)
    except DailyUpdateError as exc:
        raise error(exc) from exc


@router.get(
    "/tasks/{task_id}/reporting-context", response_model=TaskReportingContext, responses=_ERRORS
)
async def reporting_context(
    task_id: UUID, actor: ActorDependency, service: DailyUpdateServiceDependency, response: Response
) -> TaskReportingContext:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await service.context(actor, task_id)
    except DailyUpdateError as exc:
        raise error(exc) from exc
