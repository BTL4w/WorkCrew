"""Explicit JSON adapters preserve the verified immutable snapshot across packages."""

from work_management_ai.agents.reporting.contracts import ReportingSnapshot

from ..domain.snapshots import ReportMetricSnapshot


def to_reporting_snapshot(snapshot: ReportMetricSnapshot) -> ReportingSnapshot:
    if not snapshot.verified_hash():
        raise ValueError("SNAPSHOT_HASH_MISMATCH")
    value = ReportingSnapshot.model_validate(snapshot.model_dump(mode="json"))
    if not value.verified_hash():
        raise ValueError("SNAPSHOT_WIRE_DRIFT")
    return value


def to_domain_snapshot(snapshot: ReportingSnapshot) -> ReportMetricSnapshot:
    value = ReportMetricSnapshot.model_validate(snapshot.model_dump(mode="json"))
    if not snapshot.verified_hash() or not value.verified_hash():
        raise ValueError("SNAPSHOT_WIRE_DRIFT")
    return value
