"""Strictly acknowledge persisted feedback facts; no external publication."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.modules.planning_runs.domain.models import OutboxEvent
from app.modules.reporting.adapters.outbox_consumer import Publisher


class FeedbackRecorded(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"]
    feedback_id: UUID
    report_id: UUID


class FeedbackOutboxPublisher:
    def __init__(self, delegate: Publisher):
        self.delegate = delegate

    async def publish(self, event: OutboxEvent) -> None:
        if event.event_type != "feedback.recorded.v1":
            await self.delegate.publish(event)
            return
        value = FeedbackRecorded.model_validate(event.payload)
        if (
            event.envelope_version != "1.0"
            or event.aggregate_type != "feedback"
            or event.aggregate_id != value.feedback_id
        ):
            raise ValueError("INVALID_FEEDBACK_EVENT")
