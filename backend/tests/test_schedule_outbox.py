"""Configuration events are recorded without activating digest delivery."""

from dataclasses import replace
from uuid import uuid4

import pytest

from app.modules.planning_runs.domain.models import OutboxEvent
from app.modules.risk.adapters.outbox_consumer import RiskOutboxPublisher
from tests.test_risk_outbox_integration import Runner


@pytest.mark.asyncio
async def test_schedule_event_is_acknowledged_without_risk_or_digest_work():
    runner = Runner()
    publisher = RiskOutboxPublisher(runner)
    schedule_id = uuid4()
    event = OutboxEvent(
        id=uuid4(),
        event_id=uuid4(),
        organization_id=uuid4(),
        event_type="automation.schedule.changed.v1",
        aggregate_type="daily_summary_schedule",
        aggregate_id=schedule_id,
        payload={"schedule_id": str(schedule_id), "version": 1, "paused": False},
    )
    await publisher.publish(event)
    assert runner.queued == []
    for payload in (
        {**event.payload, "schedule_id": str(uuid4())},
        {**event.payload, "version": 0},
        {**event.payload, "paused": "false"},
    ):
        with pytest.raises(ValueError):
            await publisher.publish(replace(event, payload=payload))
