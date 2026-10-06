import pytest

from work_management_ai.agents.reporting.contracts import ReportingNarrative, ReportingSnapshot
from work_management_ai.agents.reporting.evaluators.numeric import verify_numeric

from .fixtures import narrative_wire, snapshot_wire


def test_fact_binding_preserves_metric_meaning():
    wire = snapshot_wire()
    snapshot = ReportingSnapshot.model_validate(wire)
    narrative = narrative_wire(wire)
    assert verify_numeric(snapshot, ReportingNarrative.model_validate(narrative)).passed
    for update in (
        {"metric_key": "tasks.deadline.overdue_count"},
        {"unit": "HOURS"},
        {"value": "9"},
        {"period": "IN_PERIOD"},
    ):
        wrong = narrative_wire(wire)
        wrong["blocks"][0]["bindings"][0].update(update)
        assert not verify_numeric(snapshot, ReportingNarrative.model_validate(wrong)).passed
    wrong = narrative_wire(wire)
    wrong["blocks"][0]["bindings"][1]["metric_key"] = "tasks.deadline.overdue_count"
    assert not verify_numeric(snapshot, ReportingNarrative.model_validate(wrong)).passed


@pytest.mark.parametrize(
    "text",
    [
        "tám Task đã xong",
        "all work complete",
        "8 tasks overdue",
        "eighty three percent likely late",
        "toàn bộ công việc đã hoàn thành",
    ],
)
def test_free_text_quantities_require_bindings(text: str):
    wire = snapshot_wire()
    doc = narrative_wire(wire)
    doc["blocks"].append(
        dict(
            id="claim",
            section="concerns",
            kind="INTERPRETATION",
            text=text,
            source_refs=[
                dict(
                    resource_type="RISK_ASSESSMENT",
                    resource_id=wire["sources"][0]["resource_id"],
                    version=3,
                    fingerprint="b" * 64,
                )
            ],
            assumptions=[],
        )
    )
    assert not verify_numeric(
        ReportingSnapshot.model_validate(wire), ReportingNarrative.model_validate(doc)
    ).passed


def test_title_and_typed_dates_are_not_quantitative_claims():
    wire = snapshot_wire()
    snapshot = ReportingSnapshot.model_validate(wire)
    assert snapshot.sources[0].label == "Hội nghị 2026"
    assert verify_numeric(snapshot, ReportingNarrative.model_validate(narrative_wire(wire))).passed


def test_source_risk_score_is_not_probability():
    wire = snapshot_wire()
    doc = narrative_wire(wire)
    doc["blocks"].append(
        dict(
            id="risk",
            section="concerns",
            kind="FACT",
            template="RISK_SCORE",
            bindings=[],
            source_bindings=[
                dict(
                    resource_type="RISK_ASSESSMENT",
                    resource_id=wire["sources"][0]["resource_id"],
                    version=3,
                    fingerprint="b" * 64,
                    field="score",
                    value="83",
                )
            ],
        )
    )
    assert verify_numeric(
        ReportingSnapshot.model_validate(wire), ReportingNarrative.model_validate(doc)
    ).passed
    doc["blocks"][-1]["source_bindings"][0]["value"] = "0.83"
    assert not verify_numeric(
        ReportingSnapshot.model_validate(wire), ReportingNarrative.model_validate(doc)
    ).passed


def test_changed_hash_unknown_sources_and_unknown_values_fail():
    wire = snapshot_wire()
    doc = narrative_wire(wire)
    doc["snapshot_hash"] = "c" * 64
    assert not verify_numeric(
        ReportingSnapshot.model_validate(wire), ReportingNarrative.model_validate(doc)
    ).passed
    snapshot = ReportingSnapshot.model_validate(wire)
    broken = snapshot.model_copy(update={"snapshot_hash": "d" * 64})
    assert not verify_numeric(
        broken, ReportingNarrative.model_validate(narrative_wire(wire))
    ).passed


def test_free_text_source_label_is_an_identifier():
    wire = snapshot_wire()
    doc = narrative_wire(wire)
    source = {
        k: wire["sources"][0][k] for k in ("resource_type", "resource_id", "version", "fingerprint")
    }
    doc["blocks"].append(
        dict(
            id="named",
            section="concerns",
            kind="INTERPRETATION",
            text="Hội nghị 2026: cần rà soát vật tư.",
            source_refs=[source],
            assumptions=[],
        )
    )
    assert verify_numeric(
        ReportingSnapshot.model_validate(wire), ReportingNarrative.model_validate(doc)
    ).passed


@pytest.mark.parametrize("template", ["METRIC", "DONE_RATIO"])
def test_fact_templates_reject_unrelated_source_bindings(template: str):
    wire = snapshot_wire()
    doc = narrative_wire(wire)
    doc["blocks"][0]["template"] = template
    if template == "METRIC":
        doc["blocks"][0]["bindings"] = doc["blocks"][0]["bindings"][:1]
    doc["blocks"][0]["source_bindings"] = [
        dict(
            resource_type="RISK_ASSESSMENT",
            resource_id=wire["sources"][0]["resource_id"],
            version=3,
            fingerprint="b" * 64,
            field="score",
            value="83",
        )
    ]
    assert not verify_numeric(
        ReportingSnapshot.model_validate(wire), ReportingNarrative.model_validate(doc)
    ).passed


@pytest.mark.parametrize("unit,basis", [("HOURS", "AT_CAPTURE"), ("COUNT", "IN_PERIOD")])
def test_done_ratio_rejects_falsely_labelled_metric_basis(unit: str, basis: str):
    import hashlib
    import json

    wire = snapshot_wire()
    doc = narrative_wire(wire)
    for metric in wire["metrics"].values():
        metric.update(unit=unit, time_basis=basis)
    for binding in doc["blocks"][0]["bindings"]:
        binding.update(unit=unit, period=basis)
    wire["snapshot_hash"] = hashlib.sha256(
        json.dumps(
            {k: v for k, v in wire.items() if k != "snapshot_hash"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()
    doc["snapshot_hash"] = wire["snapshot_hash"]
    assert not verify_numeric(
        ReportingSnapshot.model_validate(wire), ReportingNarrative.model_validate(doc)
    ).passed


@pytest.mark.parametrize(
    "unit,basis,state",
    [
        ("COUNT", "AT_CAPTURE", "KNOWN"),
        ("HOURS", "AT_CAPTURE", "KNOWN"),
        ("COUNT", "IN_PERIOD", "KNOWN"),
        ("COUNT", "AT_CAPTURE", "PARTIAL"),
        ("COUNT", "AT_CAPTURE", "STALE"),
    ],
)
def test_comparison_requires_same_known_unit_and_time_basis(unit: str, basis: str, state: str):
    import hashlib
    import json

    wire = snapshot_wire()
    wire["metrics"]["tasks.status.total_count"].update(unit=unit, time_basis=basis, state=state)
    wire["snapshot_hash"] = hashlib.sha256(
        json.dumps(
            {k: v for k, v in wire.items() if k != "snapshot_hash"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()
    doc = narrative_wire(wire)
    doc["blocks"][0].update(
        template="METRIC",
        bindings=[
            dict(
                metric_key="tasks.status.done_count",
                unit="COUNT",
                period="AT_CAPTURE",
                operator="LT",
                value="12",
                comparison_metric_key="tasks.status.total_count",
            )
        ],
    )
    assert verify_numeric(
        ReportingSnapshot.model_validate(wire), ReportingNarrative.model_validate(doc)
    ).passed == ((unit, basis, state) == ("COUNT", "AT_CAPTURE", "KNOWN"))


@pytest.mark.parametrize("state", ["READY", "STALE", "UNAVAILABLE", "PENDING"])
def test_production_risk_source_requires_ready_assessment(state: str):
    import hashlib
    import json

    wire = snapshot_wire()
    wire["sources"][0]["resource_type"] = "RISK_ASSESSMENT"
    wire["sources"][0]["facts"]["state"] = state
    wire["snapshot_hash"] = hashlib.sha256(
        json.dumps(
            {k: v for k, v in wire.items() if k != "snapshot_hash"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()
    doc = narrative_wire(wire)
    doc["blocks"] = [
        dict(
            id="risk",
            section="concerns",
            kind="FACT",
            template="RISK_SCORE",
            bindings=[],
            source_bindings=[
                dict(
                    resource_type="RISK_ASSESSMENT",
                    resource_id=wire["sources"][0]["resource_id"],
                    version=3,
                    fingerprint="b" * 64,
                    field="score",
                    value="83",
                )
            ],
        )
    ]
    assert verify_numeric(
        ReportingSnapshot.model_validate(wire), ReportingNarrative.model_validate(doc)
    ).passed == (state == "READY")


def test_vietnamese_limitation_negation_is_not_zero_quantity():
    wire = snapshot_wire()
    doc = narrative_wire(wire)
    doc["blocks"].append(
        dict(
            id="limitation",
            kind="LIMITATION",
            section="limitations",
            text="Thiếu dữ liệu effort nên không thể kết luận về tổng effort còn lại.",
            source_refs=[],
            assumptions=[],
        )
    )
    assert verify_numeric(
        ReportingSnapshot.model_validate(wire), ReportingNarrative.model_validate(doc)
    ).passed


def test_vietnamese_zero_task_claim_still_requires_binding():
    wire = snapshot_wire()
    doc = narrative_wire(wire)
    doc["blocks"].append(
        dict(
            id="limitation",
            kind="LIMITATION",
            section="limitations",
            text="không Task quá hạn",
            source_refs=[],
            assumptions=[],
        )
    )
    assert not verify_numeric(
        ReportingSnapshot.model_validate(wire), ReportingNarrative.model_validate(doc)
    ).passed


def test_included_summary_partial_binding_keeps_its_sample_meaning():
    import hashlib
    import json

    def canonical_hash(value: object) -> str:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()

    wire = snapshot_wire()
    doc = narrative_wire(wire)
    metric = wire["metrics"].pop("tasks.status.done_count")
    metric.update(
        key="included_tasks.status.done_count",
        state="PARTIAL",
        limitations=["INCLUDED_SUMMARY_ITEMS_ONLY"],
    )
    wire["metrics"][metric["key"]] = metric
    wire["query_version"] = "daily-summary-conversion.v1"
    wire["snapshot_hash"] = canonical_hash({k: v for k, v in wire.items() if k != "snapshot_hash"})
    doc["snapshot_hash"] = wire["snapshot_hash"]
    doc["blocks"] = [
        {
            "id": "included",
            "kind": "FACT",
            "section": "progress",
            "template": "METRIC",
            "bindings": [
                {
                    "metric_key": metric["key"],
                    "value": metric["value"],
                    "unit": metric["unit"],
                    "period": metric["time_basis"],
                }
            ],
        }
    ]
    assert verify_numeric(
        ReportingSnapshot.model_validate(wire), ReportingNarrative.model_validate(doc)
    ).passed
    bad_doc = narrative_wire(wire)
    bad_doc["snapshot_hash"] = wire["snapshot_hash"]
    bad_doc["blocks"] = doc["blocks"]
    from typing import Any, cast

    cast(dict[str, Any], bad_doc["blocks"][0])["bindings"][0]["metric_key"] = (
        "tasks.status.total_count"
    )
    wire["metrics"]["tasks.status.total_count"]["state"] = "PARTIAL"
    wire["snapshot_hash"] = canonical_hash({k: v for k, v in wire.items() if k != "snapshot_hash"})
    doc["snapshot_hash"] = wire["snapshot_hash"]
    assert not verify_numeric(
        ReportingSnapshot.model_validate(wire), ReportingNarrative.model_validate(doc)
    ).passed
