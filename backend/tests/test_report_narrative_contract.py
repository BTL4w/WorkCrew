from app.modules.reporting.adapters.narrative_runtime import (
    to_domain_snapshot,
    to_reporting_snapshot,
)
from app.modules.reporting.domain.narrative import FactBlock, NarrativeDocument, render_fact
from app.modules.reporting.domain.snapshots import ReportMetricSnapshot
from work_management_ai.agents.reporting.tests.fixtures import narrative_wire, snapshot_wire


def test_domain_wire_round_trip():
    wire = snapshot_wire()
    domain = ReportMetricSnapshot.model_validate(wire)
    adapted = to_reporting_snapshot(domain)
    assert adapted.model_dump(mode="json") == domain.model_dump(mode="json")
    assert to_domain_snapshot(adapted) == domain
    assert domain.verified_hash()
    doc = NarrativeDocument.model_validate(narrative_wire(wire))
    assert isinstance(doc.blocks[0], FactBlock)
    assert "8/12" in render_fact(doc.blocks[0], domain, "vi")
    assert "DONE" in render_fact(doc.blocks[0], domain, "en")


def test_full_snapshot_with_per_member_metrics_preserves_hash():
    from decimal import Decimal
    from uuid import uuid4

    from app.modules.reporting.domain.metrics import MetricState, MetricValue
    from app.modules.reporting.domain.snapshots import canonical_hash

    domain = ReportMetricSnapshot.model_validate(snapshot_wire())
    metrics = dict(domain.metrics)
    for _ in range(101):
        key = f"workload.{uuid4()}.{uuid4()}.allocated_known_subtotal_hours"
        metrics[key] = MetricValue(
            key=key,
            value=Decimal("8"),
            unit="HOURS",
            state=MetricState.KNOWN,
            time_basis="AT_CAPTURE",
        )
    payload = domain.model_copy(update={"metrics": metrics}).model_dump(
        mode="json", exclude={"snapshot_hash"}
    )
    domain = ReportMetricSnapshot.model_validate(
        {**payload, "snapshot_hash": canonical_hash(payload)}
    )
    wire = to_reporting_snapshot(domain)
    assert len(wire.metrics) == 104
    assert wire.snapshot_hash == domain.snapshot_hash
    assert to_domain_snapshot(wire) == domain
