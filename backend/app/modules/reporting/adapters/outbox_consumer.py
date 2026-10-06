"""Acknowledge generation facts already queued by the creating transaction."""

from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.modules.planning_runs.domain.models import OutboxEvent


class GenerationRequested(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"]
    report_id: UUID
    generation_id: UUID


class Publisher(Protocol):
    async def publish(self, event: OutboxEvent) -> None: ...


class ReportingOutboxPublisher:
    def __init__(self, delegate: Publisher, summaries: Publisher | None = None):
        self.delegate, self.summaries = delegate, summaries

    async def publish(self, event: OutboxEvent) -> None:
        if event.event_type == "report.generation.requested.v1":
            value = GenerationRequested.model_validate(event.payload)
            if (
                event.envelope_version != "1.0"
                or event.aggregate_type != "report"
                or value.report_id != event.aggregate_id
            ):
                raise ValueError("INVALID_REPORT_GENERATION_EVENT")
        else:
            if event.event_type == "automation.summary.captured.v1" and self.summaries is not None:
                await self.summaries.publish(event)
            await self.delegate.publish(event)
