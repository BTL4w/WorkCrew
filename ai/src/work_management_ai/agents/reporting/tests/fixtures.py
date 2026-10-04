"""Permission-safe exact metric fixture; no production context."""

import hashlib
import json
from typing import Any
from uuid import uuid4


def snapshot_wire() -> dict[str, Any]:
    stamp = "2026-10-05T04:00:00Z"
    period = dict(
        kind="DAILY",
        local_start="2026-10-05",
        local_end="2026-10-06",
        timezone="UTC",
        start_utc="2026-10-05T00:00:00Z",
        end_utc="2026-10-06T00:00:00Z",
        observed_through=stamp,
        partial_period=True,
    )
    source = dict(
        resource_type="RISK_ASSESSMENT",
        resource_id=str(uuid4()),
        version=3,
        fingerprint="b" * 64,
        observed_at=stamp,
        label="Hội nghị 2026",
        facts={"state": "READY", "score": "83", "band": "HIGH", "rationale": "Chờ vật tư"},
    )
    metrics: dict[str, Any] = {}
    for key, value in (
        ("tasks.status.done_count", "8"),
        ("tasks.status.total_count", "12"),
        ("tasks.deadline.overdue_count", "2"),
    ):
        metrics[key] = dict(
            key=key,
            value=value,
            unit="COUNT",
            state="KNOWN",
            time_basis="AT_CAPTURE",
            policy_version="report-metrics.v1",
            source_refs=[],
            limitations=[],
        )
    payload = dict(
        id=str(uuid4()),
        organization_id=str(uuid4()),
        report_id=str(uuid4()),
        project_id=str(uuid4()),
        period=period,
        captured_at=stamp,
        catalog_version="report-metrics.v1",
        query_version="report-sql.v1",
        metrics=metrics,
        sources=[source],
        receipts=[],
        limitations=[],
    )
    payload["snapshot_hash"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    return payload


def narrative_wire(snapshot: dict[str, Any], locale: str = "en") -> dict[str, Any]:
    def binding(key: str, value: str) -> dict[str, Any]:
        return dict(
            metric_key=key,
            unit="COUNT",
            period="AT_CAPTURE",
            operator="EQ",
            value=value,
            comparison_metric_key=None,
        )

    return dict(
        schema_version="1.0",
        snapshot_id=snapshot["id"],
        snapshot_hash=snapshot["snapshot_hash"],
        catalog_version="report-metrics.v1",
        locale=locale,
        blocks=[
            dict(
                id="done",
                section="progress",
                kind="FACT",
                template="DONE_RATIO",
                bindings=[
                    binding("tasks.status.done_count", "8"),
                    binding("tasks.status.total_count", "12"),
                ],
                source_bindings=[],
            )
        ],
    )
