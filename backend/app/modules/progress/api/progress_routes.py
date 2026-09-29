"""Read-only, Manager-scoped weekly progress API."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Response

from app.api.errors import ApplicationError, ErrorResponse
from app.modules.identity.api.dependencies import ActorDependency
from app.modules.progress.api.dependencies import WeeklyProgressServiceDependency
from app.modules.progress.api.progress_schemas import WeeklyProgressResponse
from app.modules.progress.application.weekly_progress_service import (
    ProgressForbiddenError,
    ProgressNotFoundError,
)

router = APIRouter(tags=["progress"])


@router.get(
    "/projects/{project_id}/weeks/{week_id}/progress",
    response_model=WeeklyProgressResponse,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
async def get_weekly_progress(
    project_id: UUID,
    week_id: UUID,
    actor: ActorDependency,
    service: WeeklyProgressServiceDependency,
    response: Response,
) -> WeeklyProgressResponse:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        result = await service.get(actor, project_id, week_id, datetime.now(UTC))
    except ProgressForbiddenError as error:
        raise ApplicationError(
            status_code=403, code="FORBIDDEN", message_key="common.error.forbidden"
        ) from error
    except ProgressNotFoundError as error:
        raise ApplicationError(
            status_code=404, code="RESOURCE_NOT_FOUND", message_key="common.error.notFound"
        ) from error
    return WeeklyProgressResponse.model_validate(result)
