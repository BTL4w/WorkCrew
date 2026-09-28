"""Authenticated binary streaming routes for private original evidence."""

from typing import Annotated, Any, NoReturn
from urllib.parse import quote, unquote
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import StreamingResponse

from app.api.errors import ApplicationError, ErrorResponse
from app.modules.identity.api.dependencies import ActorDependency
from app.modules.progress.api.dependencies import EvidenceServiceDependency
from app.modules.progress.api.evidence_schemas import EvidenceResponse
from app.modules.progress.application.evidence_service import UploadEvidenceCommand
from app.modules.progress.domain.evidence import (
    MAX_EVIDENCE_BYTES,
    EvidenceError,
    EvidenceVersionRef,
)

router = APIRouter(tags=["evidence"])
_ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (400, 401, 403, 404, 409, 410, 413, 422, 503)
}


def _raise(error: EvidenceError) -> NoReturn:
    raise ApplicationError(
        status_code=error.status, code=error.code, message_key=f"evidence.error.{error.code}"
    ) from error


@router.post(
    "/evidence",
    response_model=EvidenceResponse,
    status_code=201,
    responses=_ERRORS,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                mime: {"schema": {"type": "string", "format": "binary"}}
                for mime in (
                    "application/pdf",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "image/jpeg",
                    "image/png",
                )
            },
        }
    },
)
async def upload_evidence(
    request: Request,
    response: Response,
    actor: ActorDependency,
    service: EvidenceServiceDependency,
    filename: Annotated[str | None, Header(alias="X-Evidence-Filename")] = None,
    key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> EvidenceResponse:
    try:
        length = request.headers.get("Content-Length")
        if length is not None:
            try:
                size = int(length)
            except ValueError:
                size = -1
            if size < 0 or size > MAX_EVIDENCE_BYTES:
                error = (
                    EvidenceError("EVIDENCE_TOO_LARGE", 413)
                    if size > 0
                    else EvidenceError("INVALID_REQUEST", 400)
                )
                await service.reject(actor, str(request.state.request_id), error.code, key)
                raise error
        ref = await service.upload(
            actor,
            UploadEvidenceCommand(
                idempotency_key=key or "",
                filename=unquote(filename or ""),
                stream=request.stream(),
                mime_type=request.headers.get("Content-Type", "").split(";")[0].strip().lower(),
                request_id=str(request.state.request_id),
            ),
        )
        original = await service.get(actor, ref)
    except EvidenceError as exc:
        _raise(exc)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["ETag"] = f'"{ref.version}"'
    return EvidenceResponse.from_original(original)


@router.get(
    "/evidence/{evidence_id}/versions/{version}", response_model=EvidenceResponse, responses=_ERRORS
)
async def get_evidence(
    evidence_id: UUID,
    version: int,
    actor: ActorDependency,
    service: EvidenceServiceDependency,
    response: Response,
) -> EvidenceResponse:
    try:
        original = await service.get(actor, EvidenceVersionRef(evidence_id, version))
    except EvidenceError as exc:
        _raise(exc)
    response.headers["Cache-Control"] = "private, no-store"
    return EvidenceResponse.from_original(original)


@router.get(
    "/evidence/{evidence_id}/versions/{version}/content",
    responses=_ERRORS,
    response_class=StreamingResponse,
)
async def download_evidence(
    evidence_id: UUID,
    version: int,
    actor: ActorDependency,
    service: EvidenceServiceDependency,
    request: Request,
) -> StreamingResponse:
    try:
        result = await service.open_version(
            actor, EvidenceVersionRef(evidence_id, version), str(request.state.request_id)
        )
    except EvidenceError as exc:
        _raise(exc)
    return StreamingResponse(
        result.stream,
        media_type=result.original.blob.detected_mime,
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + quote(result.original.filename, safe=""),
            "Content-Length": str(result.original.blob.byte_length),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )
