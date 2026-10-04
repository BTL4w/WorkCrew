"""Typed HTTP contracts for Project report snapshots."""

from ..domain.commands import CreateReportCommand
from ..domain.reports import ReportDefaults, ReportPage, ReportResult, ReportSourcePage


class ReportCreateRequest(CreateReportCommand):
    pass


class ReportResponse(ReportResult):
    pass


class ReportPageResponse(ReportPage):
    pass


class ReportDefaultsResponse(ReportDefaults):
    pass


class ReportSourcesResponse(ReportSourcePage):
    pass
