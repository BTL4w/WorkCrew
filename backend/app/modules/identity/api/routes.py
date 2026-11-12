"""Versioned REST endpoints for local session authentication."""

from typing import Any

from fastapi import APIRouter, Request, Response, status

from app.api.errors import ADMISSION_ERROR_RESPONSES, ApplicationError, ErrorResponse
from app.modules.identity.api.dependencies import (
    ActorDependency,
    AuthServiceDependency,
    SettingsDependency,
)
from app.modules.identity.api.schemas import LoginRequest, MeResponse
from app.modules.identity.domain.auth import InvalidCredentialsError
from app.modules.optimization.application.login_throttle import LoginThrottled

router = APIRouter(tags=["authentication"])

_AUTH_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: ADMISSION_ERROR_RESPONSES[429],
}


@router.post(
    "/auth/login",
    response_model=MeResponse,
    responses=_AUTH_ERROR_RESPONSES,
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    service: AuthServiceDependency,
    settings: SettingsDependency,
) -> MeResponse:
    try:
        result = await service.login(
            email=payload.email,
            password=payload.password.get_secret_value(),
            request_id=str(request.state.request_id),
            client_ip=request.client.host if request.client is not None else "unknown",
        )
    except LoginThrottled as exc:
        raise ApplicationError(
            status_code=429,
            code="LOGIN_RATE_LIMITED",
            message_key="auth.login.rateLimited",
            details={"retry_after_seconds": exc.retry_after_seconds},
            retry_after_seconds=exc.retry_after_seconds,
        ) from exc
    except InvalidCredentialsError as exc:
        raise ApplicationError(
            status_code=401,
            code="INVALID_CREDENTIALS",
            message_key="auth.error.invalidCredentials",
        ) from exc

    response.set_cookie(
        key=settings.session_cookie_name,
        value=result.cookie_value,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_secure_cookie,
        samesite="lax",
        path="/",
    )
    return MeResponse.from_actor(result.actor)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    service: AuthServiceDependency,
    settings: SettingsDependency,
) -> None:
    await service.logout(
        cookie_value=request.cookies.get(settings.session_cookie_name),
        request_id=str(request.state.request_id),
    )
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        secure=settings.session_secure_cookie,
        httponly=True,
        samesite="lax",
    )


@router.get("/me", response_model=MeResponse, responses=_AUTH_ERROR_RESPONSES)
async def me(actor: ActorDependency) -> MeResponse:
    return MeResponse.from_actor(actor)
