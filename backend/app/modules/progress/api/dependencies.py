"""Evidence application service dependency."""

from typing import Annotated, cast

from fastapi import Depends, Request

from app.modules.progress.application.evidence_service import EvidenceService
from app.modules.progress.application.weekly_progress_service import WeeklyProgressService


def get_evidence_service(request: Request) -> EvidenceService:
    return cast(EvidenceService, request.app.state.evidence_service)


EvidenceServiceDependency = Annotated[EvidenceService, Depends(get_evidence_service)]


def get_weekly_progress_service(request: Request) -> WeeklyProgressService:
    return cast(WeeklyProgressService, request.app.state.weekly_progress_service)


WeeklyProgressServiceDependency = Annotated[
    WeeklyProgressService, Depends(get_weekly_progress_service)
]
