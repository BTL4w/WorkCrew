"""Exact metric meaning and source checks precede model semantic verification."""

import operator
import re
from decimal import Decimal, InvalidOperation

from work_management_ai.agents.reporting.contracts import (
    ReportingNarrative,
    ReportingSnapshot,
    SourceIdentity,
    TextBlock,
)
from work_management_ai.runtime.contracts import VerifierResult

# Conservative guard, not a proof of grounding: the model verifier also inspects every claim.
_QUANTITY = re.compile(
    r"\d|%|\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"hundred|thousand|million|percent|percentage|probability|all|every|none|most|"
    r"first|second|third|twice|half|double|triple|eighty|ninety|twenty|thirty|forty|"
    r"fifty|sixty|seventy|một|hai|ba|bốn|năm|sáu|bảy|tám|chín|mười|"
    r"trăm|nghìn|triệu|nửa|toàn bộ|tất cả|phần trăm|xác suất)\b|"
    r"\bkhông\s+(?:tasks?|công việc|giờ|blockers?)\b",
    re.IGNORECASE,
)
_OPERATORS = {
    "EQ": operator.eq,
    "LT": operator.lt,
    "LTE": operator.le,
    "GT": operator.gt,
    "GTE": operator.ge,
}


def identity(ref: SourceIdentity) -> tuple[str, object, int, str | None]:
    return ref.resource_type, ref.resource_id, ref.version, ref.fingerprint


def verify_numeric(
    snapshot: ReportingSnapshot,
    narrative: ReportingNarrative,
    *,
    source_detail_limit: int | None = None,
) -> VerifierResult:
    codes: set[str] = set()
    if (
        not snapshot.verified_hash()
        or narrative.snapshot_id != snapshot.id
        or narrative.snapshot_hash != snapshot.snapshot_hash
        or narrative.catalog_version != snapshot.catalog_version
    ):
        codes.add("SNAPSHOT_MISMATCH")
    sources = {identity(s): s for s in snapshot.sources}
    if len(sources) != len(snapshot.sources):
        codes.add("SOURCE_DUPLICATE")
    visible = {identity(s) for s in snapshot.sources[:source_detail_limit]}
    for block in narrative.blocks:
        refs = block.source_refs if isinstance(block, TextBlock) else block.source_bindings
        if any(identity(ref) not in visible for ref in refs):
            codes.add("SOURCE_DETAIL_NOT_READ")
        if isinstance(block, TextBlock):
            if any(identity(s) not in sources for s in block.source_refs):
                codes.add("SOURCE_NOT_CAPTURED")
            texts: list[str] = [block.text, *block.assumptions]
            # Only exact captured, cited labels may contain identifier digits. The
            # independent semantic pass still checks the original unmodified text.
            labels = [
                sources[identity(ref)].label
                for ref in block.source_refs
                if identity(ref) in sources
            ]
            for label in labels:
                if label:
                    texts = [text.replace(label, "SOURCE_LABEL") for text in texts]
            if any(_QUANTITY.search(text) for text in texts):
                codes.add("UNBOUND_QUANTITY")
            if re.search(
                r"https?://|\b(?:SELECT|INSERT|UPDATE|DELETE)\b", block.text, re.IGNORECASE
            ):
                codes.add("UNSAFE_EXPRESSION")
            continue
        if block.template == "DONE_RATIO":
            expected = ("tasks.status.done_count", "tasks.status.total_count")
            if (
                tuple(b.metric_key for b in block.bindings) != expected
                or block.source_bindings
                or any(
                    b.unit != "COUNT"
                    or b.period != "AT_CAPTURE"
                    or b.operator != "EQ"
                    or b.comparison_metric_key is not None
                    for b in block.bindings
                )
            ):
                codes.add("FACT_MEANING_MISMATCH")
        elif block.template == "METRIC":
            if len(block.bindings) != 1 or block.source_bindings:
                codes.add("FACT_MEANING_MISMATCH")
        elif block.template == "RISK_SCORE":
            if block.bindings or len(block.source_bindings) != 1:
                codes.add("FACT_MEANING_MISMATCH")
        for binding in block.bindings:
            metric = snapshot.metrics.get(binding.metric_key)
            if (
                metric is None
                or metric.key != binding.metric_key
                or metric.unit != binding.unit
                or metric.time_basis != binding.period
                or metric.state != "KNOWN"
                or metric.value is None
            ):
                codes.add("METRIC_BASIS_MISMATCH")
                continue
            compared = binding.value
            if binding.comparison_metric_key is not None:
                other = snapshot.metrics.get(binding.comparison_metric_key)
                if (
                    other is None
                    or other.state != "KNOWN"
                    or other.value is None
                    or other.unit != metric.unit
                    or other.time_basis != metric.time_basis
                    or other.value != binding.value
                ):
                    codes.add("COMPARISON_BASIS_MISMATCH")
                    continue
                compared = other.value
            if not _OPERATORS[binding.operator](metric.value, compared):
                codes.add("METRIC_ASSERTION_FALSE")
        for binding in block.source_bindings:
            source = sources.get(identity(binding))
            if source is None or source.resource_type != "RISK_ASSESSMENT":
                codes.add("SOURCE_NOT_CAPTURED")
                continue
            if source.facts.get("state") != "READY":
                codes.add("SOURCE_ASSESSMENT_UNAVAILABLE")
                continue
            try:
                value = source.facts.get(binding.field)
                if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                    raise ValueError("missing numeric source fact")
                if Decimal(str(value)) != binding.value:
                    codes.add("SOURCE_ASSERTION_FALSE")
            except (InvalidOperation, ValueError):
                codes.add("SOURCE_ASSERTION_FALSE")
    return VerifierResult(
        verifier_id="reporting_numeric",
        verifier_version="1.0.0",
        passed=not codes,
        safe_codes=tuple(sorted(codes)),
    )
