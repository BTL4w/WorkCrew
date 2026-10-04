import pytest
from pydantic import ValidationError

from work_management_ai.agents.reporting.contracts import ReportingNarrative

from .fixtures import narrative_wire, snapshot_wire


def test_reporting_bounds_and_authority_free_output():
    doc = narrative_wire(snapshot_wire())
    for field in ("approved", "role", "requested_handoff", "reasoning", "sql"):
        with pytest.raises(ValidationError):
            ReportingNarrative.model_validate({**doc, field: True})
    with pytest.raises(ValidationError):
        ReportingNarrative.model_validate({**doc, "blocks": doc["blocks"] * 21})


def test_context_bounds_details_without_changing_authoritative_snapshot():
    import hashlib
    import json
    from uuid import uuid4

    from work_management_ai.agents.reporting.context import model_context
    from work_management_ai.agents.reporting.contracts import ReportingContext, ReportingSnapshot

    wire = snapshot_wire()
    wire["sources"] = [{**wire["sources"][0], "resource_id": str(uuid4())} for _ in range(101)]
    wire["snapshot_hash"] = hashlib.sha256(
        json.dumps(
            {k: v for k, v in wire.items() if k != "snapshot_hash"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()
    snapshot = ReportingSnapshot.model_validate(wire)
    context = ReportingContext(
        snapshot=snapshot, base_version_id=uuid4(), locale="en", project_label="Conference"
    )
    payload = model_context(context)
    assert len(payload["snapshot"]["sources"]) == 100
    assert payload["source_details_omitted"] == 1
    projected = payload["snapshot"]["metrics"]["tasks.status.done_count"]
    assert "key" not in projected and "source_refs" not in projected
    assert projected["unit"] == snapshot.metrics["tasks.status.done_count"].unit
    assert projected["time_basis"] == snapshot.metrics["tasks.status.done_count"].time_basis
    assert payload["snapshot"]["receipts"] == snapshot.model_dump(mode="json")["receipts"]
    assert snapshot.verified_hash()
    assert len(snapshot.sources) == 101
    assert payload["snapshot"]["snapshot_hash"] == wire["snapshot_hash"]
    assert payload["snapshot"]["metrics"]["tasks.status.done_count"]["value"] == "8"


def test_total_assertion_bound_across_blocks():
    doc = narrative_wire(snapshot_wire())
    doc["blocks"][0]["template"] = "METRIC"
    doc["blocks"][0]["bindings"] = doc["blocks"][0]["bindings"][:1] * 100
    second = {**doc["blocks"][0], "id": "second", "bindings": doc["blocks"][0]["bindings"][:1]}
    with pytest.raises(ValidationError, match="assertion bound"):
        ReportingNarrative.model_validate({**doc, "blocks": [doc["blocks"][0], second]})
