"""Structured, user-safe HTTP error handling."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.modules.optimization.application.admission import AdmissionError

logger = logging.getLogger(__name__)


class FieldError(BaseModel):
    """A validation issue tied to one request field."""

    field: str
    code: str
    message_key: str


class ErrorDetail(BaseModel):
    """Stable public error fields returned by every API failure."""

    code: str
    message_key: str
    request_id: str
    field_errors: list[FieldError] = Field(default_factory=lambda: list[FieldError]())
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """Top-level error envelope."""

    error: ErrorDetail


ADMISSION_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {
        "model": ErrorResponse,
        "headers": {"Retry-After": {"schema": {"type": "integer", "minimum": 1, "maximum": 60}}},
    }
    for status in (429, 503)
}


class ApplicationError(Exception):
    """Expected application failure that is safe to map to an HTTP response."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message_key: str,
        field_errors: list[FieldError] | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message_key = message_key
        self.field_errors = list(field_errors or [])
        self.details = dict(details or {})


def _request_id(request: Request) -> str:
    return str(request.state.request_id)


def _validation_field(error: Mapping[str, Any]) -> str:
    if error.get("type") == "json_invalid":
        return "body"
    return ".".join(str(part) for part in error["loc"] if part not in {"body", "query"})


def _response(
    *,
    request: Request,
    status_code: int,
    code: str,
    message_key: str,
    field_errors: list[FieldError] | None = None,
    details: Mapping[str, Any] | None = None,
    retry_after_seconds: int | None = None,
) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message_key=message_key,
            request_id=_request_id(request),
            field_errors=field_errors or [],
            details=dict(details or {}),
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
        headers={
            "X-Request-ID": _request_id(request),
            **(
                {"Retry-After": str(max(1, min(60, retry_after_seconds)))}
                if retry_after_seconds is not None
                else {}
            ),
        },
    )


def _http_error_code(status_code: int) -> tuple[str, str]:
    return {
        400: ("INVALID_REQUEST", "common.error.invalidRequest"),
        401: ("AUTHENTICATION_REQUIRED", "common.error.authenticationRequired"),
        403: ("FORBIDDEN", "common.error.forbidden"),
        404: ("RESOURCE_NOT_FOUND", "common.error.notFound"),
        409: ("CONFLICT", "common.error.conflict"),
    }.get(status_code, ("HTTP_ERROR", "common.error.http"))


async def _audit_pending_mutation_rejection(request: Request, *, reason_code: str) -> bool:
    hook = getattr(request.state, "mutation_rejection_audit", None)
    if hook is None:
        return True
    request.state.mutation_rejection_audit = None
    try:
        await hook(reason_code=reason_code)
    except Exception:
        logger.exception("Failed to audit mutation rejection")
        return False
    return True


def _audit_unavailable_response(request: Request) -> JSONResponse:
    return _response(
        request=request,
        status_code=503,
        code="AUDIT_UNAVAILABLE",
        message_key="common.error.unexpected",
    )


def register_error_handlers(app: FastAPI) -> None:
    """Install the single public error contract for the FastAPI application."""

    @app.exception_handler(AdmissionError)
    async def handle_admission_error(  # pyright: ignore[reportUnusedFunction]
        request: Request, exc: AdmissionError
    ) -> JSONResponse:
        if not await _audit_pending_mutation_rejection(request, reason_code=exc.code):
            return _audit_unavailable_response(request)
        return _response(
            request=request,
            status_code=exc.status,
            code=exc.code,
            message_key="common.error.rateLimited"
            if exc.status == 429
            else "common.error.aiAdmissionUnavailable",
            details={"retry_after_seconds": exc.retry_after_seconds},
            retry_after_seconds=exc.retry_after_seconds,
        )

    @app.exception_handler(ApplicationError)
    async def handle_application_error(  # pyright: ignore[reportUnusedFunction]
        request: Request, exc: ApplicationError
    ) -> JSONResponse:
        if not await _audit_pending_mutation_rejection(request, reason_code=exc.code):
            return _audit_unavailable_response(request)
        return _response(
            request=request,
            status_code=exc.status_code,
            code=exc.code,
            message_key=exc.message_key,
            field_errors=exc.field_errors,
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(  # pyright: ignore[reportUnusedFunction]
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        if not await _audit_pending_mutation_rejection(request, reason_code="VALIDATION_FAILED"):
            return _audit_unavailable_response(request)
        field_errors = [
            FieldError(
                field=_validation_field(error),
                code=str(error["type"]).upper(),
                message_key="validation.invalid",
            )
            for error in exc.errors()
        ]
        return _response(
            request=request,
            status_code=422,
            code="VALIDATION_FAILED",
            message_key="common.error.validation",
            field_errors=field_errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(  # pyright: ignore[reportUnusedFunction]
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code, message_key = _http_error_code(exc.status_code)
        return _response(
            request=request,
            status_code=exc.status_code,
            code=code,
            message_key=message_key,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(  # pyright: ignore[reportUnusedFunction]
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception("Unhandled application error", exc_info=exc)
        return _response(
            request=request,
            status_code=500,
            code="INTERNAL_ERROR",
            message_key="common.error.unexpected",
        )
