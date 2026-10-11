"""Planning AI runtime boundary tests."""

import json
from pathlib import Path
from typing import cast

from app.modules.planning_runs.adapters.ai_runtime import PlanningAIRuntime

_PLANNING_FIXTURE = Path(__file__).parents[2] / "ai" / "tests" / "fixtures" / "planning_vi.json"


def test_validated_proposal_content_is_json_serializable() -> None:
    content = cast(dict[str, object], json.loads(_PLANNING_FIXTURE.read_text()))

    normalized = PlanningAIRuntime().validate_proposal_content(content)

    project = cast(dict[str, object], normalized["project"])
    assert project["start_date"] == "2026-08-10"
    assert project["due_date"] == "2026-09-30"
    assert json.loads(json.dumps(normalized))["project"] == project


def test_progressive_entry_is_versioned_and_opt_in_for_legacy_compatibility() -> None:
    from app.core.config import Settings

    assert Settings(environment="test").progressive_planning_enabled is False
    assert PlanningAIRuntime(progressive_enabled=True).workflow_version == "3.0.0"
    assert PlanningAIRuntime().workflow_version == "2.0.0"
