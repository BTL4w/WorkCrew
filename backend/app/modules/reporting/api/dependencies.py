from typing import Annotated, cast

from fastapi import Depends, Request

from ..application.generation_service import GenerationService
from ..application.report_service import ReportService


def get_report_service(request: Request) -> ReportService:
    return cast(ReportService, request.app.state.reporting_service)


ReportServiceDependency = Annotated[ReportService, Depends(get_report_service)]


def get_generation_service(request: Request) -> GenerationService:
    return cast(GenerationService, request.app.state.report_generation_service)


GenerationServiceDependency = Annotated[GenerationService, Depends(get_generation_service)]
