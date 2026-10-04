"""Bound model source details without altering captured aggregates or hash."""

from typing import Any

from work_management_ai.agents.reporting.contracts import ReportingContext

SOURCE_DETAIL_LIMIT = 100


def model_context(context: ReportingContext) -> dict[str, Any]:
    payload = context.model_dump(mode="json")
    snapshot = payload["snapshot"]
    # The full authorized immutable source index stays outside provider context.
    # Metrics retain their exact values and aggregate receipts. Never recompute a
    # hash over this projection: the original hash binds the authoritative facts.
    snapshot["sources"] = snapshot["sources"][:SOURCE_DETAIL_LIMIT]
    for metric in snapshot["metrics"].values():
        metric.pop("source_refs", None)
        # The map key already carries the metric identifier. Avoid charging this
        # duplicate text on generation, grounding and a transient retry.
        metric.pop("key", None)
        if not metric["limitations"]:
            metric.pop("limitations")
    payload["source_details_omitted"] = max(0, len(context.snapshot.sources) - SOURCE_DETAIL_LIMIT)
    return payload
