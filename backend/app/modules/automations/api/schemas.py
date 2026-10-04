"""Typed configuration requests, independent of AI chat intent."""

from pydantic import Field

from ..domain.schedules import Contract, ScheduleCommand


class SchedulePreviewRequest(Contract):
    command: ScheduleCommand
    expected_version: int = Field(ge=0)
