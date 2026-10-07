from datetime import UTC, datetime, timedelta

import pytest

from app.modules.feedback.domain.retention import PayloadClass, expires_at


@pytest.mark.parametrize(
    "classification,ceiling", [(PayloadClass.RAW_CONTEXT, 30), (PayloadClass.REDACTED_TRACE, 90)]
)
def test_exact_maximum_and_configured_shorter_retention(classification: PayloadClass, ceiling: int):
    born = datetime(2026, 1, 1, tzinfo=UTC)
    assert expires_at(born, classification, ceiling) == born + timedelta(days=ceiling)
    assert expires_at(born, classification, 0) == born
    with pytest.raises(ValueError, match="INVALID_RETENTION_POLICY"):
        expires_at(born, classification, ceiling + 1)
