"""Capture a report through the transaction-bound source port."""

from ..domain.commands import CaptureReportCommand
from ..domain.snapshots import ReportMetricSnapshot
from .ports import ReportSnapshotReadPort


class ReportSnapshotService:
    def __init__(self, reader: ReportSnapshotReadPort):
        self.reader = reader

    async def capture(self, command: CaptureReportCommand) -> ReportMetricSnapshot:
        result = await self.reader.capture(command)
        if not result.verified_hash():
            raise ValueError("invalid snapshot hash")
        return result
