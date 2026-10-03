"""Typed dispatcher rejects unknown events and revalidates recipients."""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from app.modules.planning_runs.domain.models import OutboxEvent
from app.modules.risk.adapters.outbox_consumer import RiskOutboxPublisher


class Runner:
    def __init__(self):
        self.queued: list[UUID] = []

    async def queue_event(self, event: OutboxEvent):
        self.queued.append(event.id)

    async def deliver(self, event: OutboxEvent):
        self.queued.append(event.id)


@pytest.mark.asyncio
async def test_unknown_event_explicit_failure_and_typed_envelope():
    runner = Runner()
    publisher = RiskOutboxPublisher(runner)
    event = OutboxEvent(
        id=uuid4(),
        event_id=uuid4(),
        organization_id=uuid4(),
        event_type="unknown.event.v1",
        aggregate_type="task",
        aggregate_id=uuid4(),
        payload={},
    )
    with pytest.raises(NotImplementedError):
        await publisher.publish(event)
    event = replace(
        event, event_type="daily_update.confirmed", payload={"task_ids": [str(uuid4())]}
    )
    await publisher.publish(event)
    assert runner.queued == [event.id]
    with pytest.raises(ValueError):
        await publisher.publish(replace(event, envelope_version="2.0"))
