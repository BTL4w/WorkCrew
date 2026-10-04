"""Manager-scoped report REST APIs; all reads are private, never cached."""

import re
from collections.abc import Callable, Coroutine
from functools import partial
from inspect import isawaitable
from typing import Annotated, Any, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.routing import APIRoute

from app.api.errors import ApplicationError, ErrorResponse
from app.core.config import Settings
from app.modules.identity.api.dependencies import ActorDependency, get_authenticated_actor
from app.modules.identity.application.auth_service import AuthService

from ..domain.commands import GenerateNarrativeCommand
from ..domain.reports import ReportError
from .dependencies import (
    GenerationServiceDependency,
    ReportServiceDependency,
    get_generation_service,
    get_report_service,
)
from .schemas import (
    ReportCreateRequest,
    ReportDefaultsResponse,
    ReportPageResponse,
    ReportPublishRequest,
    ReportResponse,
    ReportSourcesResponse,
)


class ReportRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def preflight(request: Request) -> Response:
            if request.method == "POST" and request.url.path.endswith(("/publish", "/generate")):
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
                try:
                    report_id = UUID(str(request.path_params.get("report_id", "")))
                except ValueError:
                    report_id = None
                if request.url.path.endswith("/generate"):
                    request.state.mutation_rejection_audit = partial(
                        get_generation_service(request).audit_rejection,
                        actor=actor,
                        key=request.headers.get("Idempotency-Key"),
                        report_id=report_id,
                    )
                else:
                    request.state.mutation_rejection_audit = partial(
                        get_report_service(request).audit_publish_rejection,
                        actor=actor,
                        request_id=str(request.state.request_id),
                        key=request.headers.get("Idempotency-Key"),
                        report_id=report_id,
                    )
            return await handler(request)

        return preflight


router = APIRouter(prefix="/reports", tags=["reports"], route_class=ReportRoute)
IdempotencyHeader = Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)]
_ERRORS: dict[int | str, dict[str, Any]] = {
    n: {"model": ErrorResponse} for n in (400, 401, 403, 404, 409, 412, 422, 428)
}


def _raise(error: ReportError) -> NoReturn:
    key = {403: "forbidden", 404: "notFound", 409: "idempotencyKeyReused", 422: "validation"}.get(
        error.status, "invalidRequest"
    )
    raise ApplicationError(
        status_code=error.status, code=error.code, message_key=f"common.error.{key}"
    ) from error


@router.post("", response_model=ReportResponse, status_code=201, responses=_ERRORS)
async def create_report(
    body: ReportCreateRequest,
    actor: ActorDependency,
    service: ReportServiceDependency,
    key: IdempotencyHeader,
    request: Request,
    response: Response,
) -> ReportResponse:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        result = await service.create(
            actor=actor, command=body, idempotency_key=key, request_id=request.state.request_id
        )
    except ReportError as exc:
        _raise(exc)
    response.headers["ETag"] = f'"{result.report.version}"'
    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return ReportResponse.model_validate(result.model_dump())


@router.get("", response_model=ReportPageResponse, responses=_ERRORS)
async def list_reports(
    actor: ActorDependency,
    service: ReportServiceDependency,
    response: Response,
    project_id: UUID,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ReportPageResponse:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        result = await service.list(
            actor=actor, project_id=project_id, page=page, page_size=page_size
        )
    except ReportError as exc:
        _raise(exc)
    return ReportPageResponse.model_validate(result.model_dump())


@router.get("/defaults", response_model=ReportDefaultsResponse, responses=_ERRORS)
async def report_defaults(
    project_id: UUID, actor: ActorDependency, service: ReportServiceDependency, response: Response
) -> ReportDefaultsResponse:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        result = await service.defaults(actor=actor, project_id=project_id)
    except ReportError as exc:
        _raise(exc)
    return ReportDefaultsResponse.model_validate(result.model_dump())


@router.get("/{report_id}", response_model=ReportResponse, responses=_ERRORS)
async def get_report(
    report_id: UUID, actor: ActorDependency, service: ReportServiceDependency, response: Response
) -> ReportResponse:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        result = await service.get(actor=actor, report_id=report_id)
    except ReportError as exc:
        _raise(exc)
    response.headers["ETag"] = f'"{result.report.version}"'
    return ReportResponse.model_validate(result.model_dump())


@router.get("/{report_id}/sources", response_model=ReportSourcesResponse, responses=_ERRORS)
async def get_report_sources(
    report_id: UUID,
    actor: ActorDependency,
    service: ReportServiceDependency,
    response: Response,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ReportSourcesResponse:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        result = await service.sources(
            actor=actor, report_id=report_id, cursor=cursor, page_size=page_size
        )
    except ReportError as exc:
        _raise(exc)
    return ReportSourcesResponse.model_validate(result.model_dump())


@router.post(
    "/{report_id}/publish", response_model=ReportResponse, status_code=201, responses=_ERRORS
)
async def publish_report(
    report_id: UUID,
    body: ReportPublishRequest,
    actor: ActorDependency,
    service: ReportServiceDependency,
    key: IdempotencyHeader,
    request: Request,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ReportResponse:
    if if_match is None:
        _raise(ReportError("PRECONDITION_REQUIRED", 428))
    match = re.fullmatch(r'"([1-9][0-9]{0,9})"', if_match)
    if match is None:
        _raise(ReportError("INVALID_REQUEST", 400))
    request.state.mutation_rejection_audit = None
    try:
        result = await service.publish_metrics(
            actor=actor,
            report_id=report_id,
            command=body,
            expected_version=int(match.group(1)),
            idempotency_key=key,
            request_id=request.state.request_id,
        )
    except ReportError as exc:
        _raise(exc)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["ETag"] = f'"{result.report.version}"'
    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return ReportResponse.model_validate(result.model_dump())


@router.post(
    "/{report_id}/generate", response_model=ReportResponse, status_code=202, responses=_ERRORS
)
async def generate_report(
    report_id: UUID,
    body: GenerateNarrativeCommand,
    actor: ActorDependency,
    service: GenerationServiceDependency,
    key: IdempotencyHeader,
    request: Request,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ReportResponse:
    if if_match is None:
        _raise(ReportError("PRECONDITION_REQUIRED", 428))
    match = re.fullmatch(r'"([1-9][0-9]{0,9})"', if_match)
    if match is None:
        _raise(ReportError("INVALID_REQUEST", 400))
    request.state.mutation_rejection_audit = None
    try:
        result = await service.request(
            actor=actor,
            report_id=report_id,
            command=body,
            expected_version=int(match.group(1)),
            idempotency_key=key,
        )
    except ReportError as exc:
        _raise(exc)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["ETag"] = f'"{result.report.version}"'
    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return ReportResponse.model_validate(result.model_dump())
