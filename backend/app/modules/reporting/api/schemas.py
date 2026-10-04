"""Typed HTTP contracts for Project report snapshots."""

from ..domain.commands import CreateReportCommand, PublishReportCommand
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


class ReportPublishRequest(PublishReportCommand):
    pass
