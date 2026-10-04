"""The worker must resolve new trigger FKs without importing the API composition."""

import subprocess
import sys
from pathlib import Path


def test_worker_metadata_resolves_trigger_targets_without_api_composition():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.modules.assistant.adapters.database_models "
            "import OrchestrationRunModel; "
            "assert all(fk.column.table is not None "
            "for fk in OrchestrationRunModel.__table__.foreign_keys)",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
