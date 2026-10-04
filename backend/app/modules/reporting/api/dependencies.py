from typing import Annotated, cast

from fastapi import Depends, Request

from ..application.report_service import ReportService


def get_report_service(request: Request) -> ReportService:
    return cast(ReportService, request.app.state.reporting_service)


ReportServiceDependency = Annotated[ReportService, Depends(get_report_service)]
