"""Typed narrative data and deterministic fact rendering, without model calls."""

from typing import Literal

from work_management_ai.agents.reporting.contracts import (
    FactBlock as FactBlock,
)
from work_management_ai.agents.reporting.contracts import (
    ReportingNarrative as NarrativeDocument,
)
from work_management_ai.agents.reporting.contracts import (
    ReportingSnapshot,
)
from work_management_ai.agents.reporting.evaluators.numeric import verify_numeric

from .snapshots import ReportMetricSnapshot

_LABELS = {
    "tasks.status.done_count": ("Task ở DONE", "Tasks in DONE"),
    "tasks.status.total_count": ("Tổng số Task", "Total Tasks"),
    "tasks.deadline.overdue_count": ("Task quá hạn", "Overdue Tasks"),
}


def render_fact(
    block: FactBlock, snapshot: ReportMetricSnapshot, locale: Literal["vi", "en"]
) -> str:
    wire = ReportingSnapshot.model_validate(snapshot.model_dump(mode="json"))
    document = NarrativeDocument(
        snapshot_id=snapshot.id,
        snapshot_hash=snapshot.snapshot_hash,
        locale=locale,
        blocks=(block,),
    )
    if not verify_numeric(wire, document).passed:
        raise ValueError("FACT_VERIFICATION_FAILED")
    if block.template == "DONE_RATIO":
        done = snapshot.metrics["tasks.status.done_count"].value
        total = snapshot.metrics["tasks.status.total_count"].value
        return (
            f"{done}/{total} Tasks ở trạng thái DONE tại thời điểm chụp."
            if locale == "vi"
            else f"{done}/{total} Tasks in DONE at capture."
        )
    if block.template == "RISK_SCORE":
        score = block.source_bindings[0].value
        return (
            f"Điểm rủi ro do AI đánh giá: {score}/100."
            if locale == "vi"
            else f"AI-assessed risk score: {score}/100."
        )
    binding = block.bindings[0]
    metric = snapshot.metrics[binding.metric_key]
    label = _LABELS.get(binding.metric_key, (binding.metric_key, binding.metric_key))[
        0 if locale == "vi" else 1
    ]
    comparison = {"EQ": "=", "LT": "<", "LTE": "≤", "GT": ">", "GTE": "≥"}[binding.operator]
    basis = {
        "AT_CAPTURE": ("tại thời điểm chụp", "at capture"),
        "IN_PERIOD": ("trong kỳ", "in period"),
        "DECLARED_REPORTING_DATE": ("theo ngày báo cáo gốc", "by declared reporting date"),
    }
    time_label = basis[binding.period][0 if locale == "vi" else 1]
    return f"{label}: {metric.value} {metric.unit} {comparison} {binding.value} ({time_label})."
