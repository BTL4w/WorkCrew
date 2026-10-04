"""Manager-scoped report REST APIs; all reads are private, never cached."""

from typing import Annotated, Any, NoReturn
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response

from app.api.errors import ApplicationError, ErrorResponse
from app.modules.identity.api.dependencies import ActorDependency

from ..domain.reports import ReportError
from .dependencies import ReportServiceDependency
from .schemas import (
    ReportCreateRequest,
    ReportDefaultsResponse,
    ReportPageResponse,
    ReportResponse,
    ReportSourcesResponse,
)

router = APIRouter(prefix="/reports", tags=["reports"])
IdempotencyHeader = Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)]
_ERRORS: dict[int | str, dict[str, Any]] = {
    n: {"model": ErrorResponse} for n in (401, 403, 404, 409, 422)
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
