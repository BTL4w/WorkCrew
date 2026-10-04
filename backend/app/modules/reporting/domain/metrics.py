"""Versioned metric values and immutable source/query provenance."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Self
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

CATALOG_VERSION = "report-metrics.v1"
QUERY_VERSION = "report-sql.v1"


class ReportContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MetricState(StrEnum):
    KNOWN = "KNOWN"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"
    STALE = "STALE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class SourceRef(ReportContract):
    resource_type: str
    resource_id: UUID
    version: int = Field(ge=1)
    fingerprint: str | None = None
    observed_at: datetime
    label: str | None = None
    facts: dict[str, JsonValue] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def preserve_captured_wire(self, handler: SerializerFunctionWrapHandler):
        payload: dict[str, Any] = handler(self)
        # Earlier immutable snapshots lack these additive fields; keep their original hash.
        for field in ("label", "facts"):
            if field not in self.model_fields_set:
                payload.pop(field, None)
        return payload


class MetricValue(ReportContract):
    key: str
    value: Decimal | None
    unit: Literal["COUNT", "HOURS", "FRACTION", "PERCENT", "SCORE", "DAYS"]
    state: MetricState
    time_basis: Literal["AT_CAPTURE", "IN_PERIOD", "DECLARED_REPORTING_DATE"]
    policy_version: str = CATALOG_VERSION
    source_refs: tuple[SourceRef, ...] = ()
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_value_state(self) -> Self:
        if self.value is not None and (not self.value.is_finite() or self.value < 0):
            raise ValueError("metrics must be finite and nonnegative")
        if self.state is MetricState.KNOWN and self.value is None:
            raise ValueError("known metric requires a value")
        if (
            self.state in {MetricState.UNKNOWN, MetricState.NOT_APPLICABLE}
            and self.value is not None
        ):
            raise ValueError("unknown/not-applicable metric cannot carry a value")
        return self


class AggregateReceipt(ReportContract):
    id: UUID
    project_id: UUID
    query_version: str = QUERY_VERSION
    catalog_version: str = CATALOG_VERSION
    metric_keys: tuple[str, ...]
    row_count: int = Field(ge=0)
    captured_at: datetime
    isolation: Literal["repeatable read"] = "repeatable read"
    scope_hash: str
