"""Evidence application service dependency."""

from typing import Annotated, cast

from fastapi import Depends, Request

from app.modules.progress.application.evidence_service import EvidenceService


def get_evidence_service(request: Request) -> EvidenceService:
    return cast(EvidenceService, request.app.state.evidence_service)


EvidenceServiceDependency = Annotated[EvidenceService, Depends(get_evidence_service)]
