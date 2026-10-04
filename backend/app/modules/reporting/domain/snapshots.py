"""Immutable captured metric payloads; hash covers facts and provenance."""

import hashlib
import json
from datetime import datetime
from uuid import UUID

from pydantic import Field

from .metrics import (
    CATALOG_VERSION,
    QUERY_VERSION,
    AggregateReceipt,
    MetricValue,
    ReportContract,
    SourceRef,
)
from .periods import ReportPeriod


def canonical_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


class ReportMetricSnapshot(ReportContract):
    id: UUID
    organization_id: UUID
    report_id: UUID
    project_id: UUID
    period: ReportPeriod
    captured_at: datetime
    catalog_version: str = CATALOG_VERSION
    query_version: str = QUERY_VERSION
    snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    metrics: dict[str, MetricValue]
    sources: tuple[SourceRef, ...] = ()
    receipts: tuple[AggregateReceipt, ...] = ()
    limitations: tuple[str, ...] = ()

    def verified_hash(self) -> bool:
        return self.snapshot_hash == canonical_hash(
            self.model_dump(mode="json", exclude={"snapshot_hash"})
        )
