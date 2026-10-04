"""Additive captured-source details must preserve legacy immutable payloads."""

from datetime import UTC, datetime
from uuid import uuid4

from app.modules.reporting.domain.metrics import SourceRef
from app.modules.reporting.domain.snapshots import canonical_hash


def test_legacy_source_roundtrip_preserves_original_hash():
    legacy = {
        "resource_type": "TASK",
        "resource_id": str(uuid4()),
        "version": 1,
        "fingerprint": "a" * 64,
        "observed_at": "2026-10-04T12:00:00Z",
    }
    captured = SourceRef.model_validate(legacy)
    assert captured.model_dump(mode="json") == legacy
    assert canonical_hash(captured.model_dump(mode="json")) == canonical_hash(legacy)
    enriched = SourceRef(
        resource_type="TASK",
        resource_id=uuid4(),
        version=1,
        observed_at=datetime(2026, 10, 4, 12, tzinfo=UTC),
        label="Survey",
        facts={"status": "DONE"},
    )
    assert SourceRef.model_validate(enriched.model_dump(mode="json")) == enriched
