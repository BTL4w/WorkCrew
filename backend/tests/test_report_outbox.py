"""Metric capture events acknowledge committed facts, never invoke a provider."""

from dataclasses import replace
from uuid import uuid4

import pytest

from app.modules.planning_runs.domain.models import OutboxEvent
from app.modules.risk.adapters.outbox_consumer import RiskOutboxPublisher
from tests.test_risk_outbox_integration import Runner


@pytest.mark.asyncio
async def test_metric_capture_event_is_typed_and_not_a_risk_trigger():
    runner = Runner()
    report_id = uuid4()
    event = OutboxEvent(
        id=uuid4(),
        event_id=uuid4(),
        organization_id=uuid4(),
        event_type="report.metrics_captured.v1",
        aggregate_type="report",
        aggregate_id=report_id,
        payload={
            "schema_version": "1.0",
            "report_id": str(report_id),
            "snapshot_id": str(uuid4()),
            "snapshot_hash": "a" * 64,
            "actor_membership_id": str(uuid4()),
        },
    )
    publisher = RiskOutboxPublisher(runner)
    await publisher.publish(event)
    assert runner.queued == []
    for payload in (
        {**event.payload, "report_id": str(uuid4())},
        {**event.payload, "snapshot_hash": "invalid"},
        {**event.payload, "actor_membership_id": "fake"},
        {**event.payload, "schema_version": "2.0"},
    ):
        with pytest.raises(ValueError):
            await publisher.publish(replace(event, payload=payload))
    with pytest.raises(NotImplementedError):
        await publisher.publish(replace(event, event_type="report.unknown"))
