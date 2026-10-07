from datetime import UTC, datetime, timedelta

import pytest

from work_management_ai.runtime.memory_manager import MemoryManager, RuntimeMemoryError


def test_checkpoint_expiry_boundary():
    now = datetime(2026, 10, 7, tzinfo=UTC)
    memory = MemoryManager()
    checkpoint = memory.checkpoint({"result": "safe"}, expires_at=now)
    assert memory.restore(checkpoint, now=now - timedelta(microseconds=1)) == {"result": "safe"}
    with pytest.raises(RuntimeMemoryError, match="CONTEXT_EXPIRED"):
        memory.restore(checkpoint, now=now)
