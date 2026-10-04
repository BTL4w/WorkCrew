"""Provider-free report wire contracts; authority stays in application services."""

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

type Unit = Literal["COUNT", "HOURS", "FRACTION", "PERCENT", "SCORE", "DAYS"]
type TimeBasis = Literal["AT_CAPTURE", "IN_PERIOD", "DECLARED_REPORTING_DATE"]
type Section = Literal["summary", "progress", "concerns", "recommendations", "limitations"]
type SafeCode = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReportingPeriod(Contract):
    kind: Literal["DAILY", "WEEKLY"]
    local_start: date
    local_end: date
    timezone: str
    start_utc: datetime
    end_utc: datetime
    observed_through: datetime
    partial_period: bool


class SourceIdentity(Contract):
    resource_type: str = Field(min_length=1, max_length=100)
    resource_id: UUID
    version: int = Field(ge=1)
    fingerprint: str | None = Field(default=None, max_length=128)


class ReportingSource(SourceIdentity):
    observed_at: datetime
    label: str | None = Field(default=None, max_length=2000)
    facts: dict[str, JsonValue] = Field(default_factory=dict, max_length=100)

    @model_serializer(mode="wrap")
    def preserve_captured_wire(self, handler: SerializerFunctionWrapHandler):
        payload: dict[str, Any] = handler(self)
        for field in ("label", "facts"):
            if field not in self.model_fields_set:
                payload.pop(field, None)
        return payload

    @property
    def identity(self) -> tuple[str, UUID, int, str | None]:
        return self.resource_type, self.resource_id, self.version, self.fingerprint


class ReportingMetric(Contract):
    key: str = Field(min_length=1, max_length=256)
    value: Decimal | None
    unit: Unit
    state: Literal["KNOWN", "PARTIAL", "UNKNOWN", "STALE", "NOT_APPLICABLE"]
    time_basis: TimeBasis
    policy_version: str = "report-metrics.v1"
    source_refs: tuple[ReportingSource, ...] = ()
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def finite_metric(self) -> Self:
        if self.value is not None and (not self.value.is_finite() or self.value < 0):
            raise ValueError("invalid metric value")
        if self.state == "KNOWN" and self.value is None:
            raise ValueError("known metric requires value")
        if self.state in {"UNKNOWN", "NOT_APPLICABLE"} and self.value is not None:
            raise ValueError("unknown metric cannot carry value")
        return self


class ReportingReceipt(Contract):
    id: UUID
    project_id: UUID
    query_version: str = "report-sql.v1"
    catalog_version: str = "report-metrics.v1"
    metric_keys: tuple[str, ...]
    row_count: int = Field(ge=0)
    captured_at: datetime
    isolation: Literal["repeatable read"] = "repeatable read"
    scope_hash: str


class ReportingSnapshot(Contract):
    id: UUID
    organization_id: UUID
    report_id: UUID
    project_id: UUID
    period: ReportingPeriod
    captured_at: datetime
    catalog_version: Literal["report-metrics.v1"] = "report-metrics.v1"
    query_version: Literal["report-sql.v1"] = "report-sql.v1"
    snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    metrics: dict[str, ReportingMetric]
    sources: tuple[ReportingSource, ...] = ()
    receipts: tuple[ReportingReceipt, ...] = Field(default=(), max_length=100)
    limitations: tuple[str, ...] = ()

    def verified_hash(self) -> bool:
        payload = self.model_dump(mode="json", exclude={"snapshot_hash"})
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()
        return digest == self.snapshot_hash


class MetricBinding(Contract):
    metric_key: str = Field(min_length=1, max_length=256)
    unit: Unit
    period: TimeBasis
    operator: Literal["EQ", "LT", "LTE", "GT", "GTE"] = "EQ"
    value: Decimal = Field(ge=0, allow_inf_nan=False)
    comparison_metric_key: str | None = Field(default=None, max_length=256)


class SourceBinding(SourceIdentity):
    field: Literal["score"]
    value: Decimal = Field(ge=0, le=100, allow_inf_nan=False)


class FactBlock(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    section: Section
    kind: Literal["FACT"] = "FACT"
    template: Literal["METRIC", "DONE_RATIO", "RISK_SCORE"]
    bindings: tuple[MetricBinding, ...] = Field(default=(), max_length=100)
    source_bindings: tuple[SourceBinding, ...] = Field(default=(), max_length=100)


class NarrativeTextBlock(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    section: Section
    kind: Literal["INTERPRETATION", "RECOMMENDATION", "LIMITATION"]
    text: str = Field(min_length=1, max_length=2000)
    source_refs: tuple[SourceIdentity, ...] = Field(max_length=100)
    assumptions: tuple[Annotated[str, Field(min_length=1, max_length=500)], ...] = Field(
        default=(), max_length=10
    )

    @model_validator(mode="after")
    def cited_analysis(self) -> Self:
        if self.kind != "LIMITATION" and not self.source_refs:
            raise ValueError("analysis requires sources")
        return self


TextBlock = NarrativeTextBlock

type NarrativeBlock = Annotated[FactBlock | TextBlock, Field(discriminator="kind")]


class ReportingNarrative(Contract):
    schema_version: Literal["1.0"] = "1.0"
    snapshot_id: UUID
    snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    catalog_version: Literal["report-metrics.v1"] = "report-metrics.v1"
    locale: Literal["vi", "en"]
    blocks: tuple[NarrativeBlock, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def global_bounds(self) -> Self:
        if len({b.id for b in self.blocks}) != len(self.blocks):
            raise ValueError("duplicate block ids")
        assertions = sum(
            len(b.bindings) + len(b.source_bindings)
            for b in self.blocks
            if isinstance(b, FactBlock)
        )
        if assertions > 100:
            raise ValueError("assertion bound exceeded")
        return self


class ClaimVerdict(Contract):
    block_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    grounded: bool
    quantities_bound: bool
    no_unsupported_cause_or_forecast: bool
    safe_codes: tuple[SafeCode, ...] = Field(max_length=10)


class SemanticVerdict(Contract):
    passed: bool
    claim_verdicts: tuple[ClaimVerdict, ...] = Field(max_length=20)
    safe_codes: tuple[SafeCode, ...] = Field(max_length=10)


class ReportingContext(Contract):
    snapshot: ReportingSnapshot
    base_version_id: UUID
    locale: Literal["vi", "en"]
    project_label: str = Field(max_length=2000)


class ReportingRequest(Contract):
    kind: Literal["REPORT_REQUEST", "SUMMARY_JOB"]
    report_id: UUID
    base_version_id: UUID
    snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_key: str = Field(min_length=16, max_length=128)
    summary_id: UUID | None = None
    locale: Literal["vi", "en"]

    @model_validator(mode="after")
    def summary_shape(self) -> Self:
        if (self.kind == "SUMMARY_JOB") != (self.summary_id is not None):
            raise ValueError("summary trigger shape")
        return self


class ReportingProposal(Contract):
    request: ReportingRequest
    narrative: ReportingNarrative
    semantic_verdict: SemanticVerdict
    agent_version: Literal["1.0.0"] = "1.0.0"
    workflow_version: Literal["reporting-narrative.v1"] = "reporting-narrative.v1"
    prompt_version: Literal["reporting.system.v1"] = "reporting.system.v1"
    grounding_prompt_version: Literal["reporting.grounding.v1"] = "reporting.grounding.v1"
    numeric_verifier_version: Literal["1.0.0"] = "1.0.0"
    semantic_verifier_version: Literal["1.0.0"] = "1.0.0"
    manifest_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    skill_versions: tuple[str, ...]
    tool_versions: tuple[str, ...]
    model_refs: tuple[str, ...]


class ReportingUsageScope(Contract):
    organization_id: UUID
    membership_id: UUID
    generation_id: UUID
    fence: int = Field(ge=1)
    worker_id: str = Field(min_length=1, max_length=128)


class ReportingUsage(Contract):
    attempts: int = Field(ge=0, le=3)
    input_reserved: int = Field(ge=0, le=48000)
    output_reserved: int = Field(ge=0, le=8000)
    tools: int = Field(ge=0, le=6)
    retries: int = Field(ge=0, le=1)
