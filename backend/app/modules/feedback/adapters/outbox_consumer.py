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


class FeedbackOutcomeRecorded(FeedbackRecorded):
    outcome_id: UUID


class EvaluationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"]
    id: UUID
    policy_version: Literal["report-eval-provider.v1", "report-eval-dataset.v1"]


class FeedbackOutboxPublisher:
    def __init__(self, delegate: Publisher):
        self.delegate = delegate

    async def publish(self, event: OutboxEvent) -> None:
        if event.event_type in (
            "evaluation.prepare.v1",
            "evaluation.curate.v1",
            "evaluation.freeze.v1",
            "evaluation.run.requested.v1",
            "evaluation.run.completed.v1",
        ):
            value = EvaluationEvent.model_validate(event.payload)
            expected_policy = (
                "report-eval-provider.v1"
                if event.event_type.startswith("evaluation.run.")
                else "report-eval-dataset.v1"
            )
            if (
                event.envelope_version != "1.0"
                or event.aggregate_type != "evaluation"
                or event.aggregate_id != value.id
                or value.policy_version != expected_policy
            ):
                raise ValueError("INVALID_EVALUATION_EVENT")
            return
        if event.event_type not in ("feedback.recorded.v1", "feedback.outcome.recorded.v1"):
            await self.delegate.publish(event)
            return
        value = (
            FeedbackOutcomeRecorded
            if event.event_type == "feedback.outcome.recorded.v1"
            else FeedbackRecorded
        ).model_validate(event.payload)
        if (
            event.envelope_version != "1.0"
            or event.aggregate_type != "feedback"
            or event.aggregate_id != value.feedback_id
        ):
            raise ValueError("INVALID_FEEDBACK_EVENT")
