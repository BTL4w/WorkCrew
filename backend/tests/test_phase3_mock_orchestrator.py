# pyright: reportPrivateUsage=false

from typing import cast

from app.modules.planning_runs.adapters.ai_runtime import _mock_plan, _Phase2MockModelGateway


def test_mock_provider_keeps_combined_team_step_behind_project_plan() -> None:
    catalog: list[object] = [
        {"agent_id": "planning", "capabilities": ["planning.create"]},
        {"agent_id": "assignment", "capabilities": ["assignment.recommend_team"]},
    ]
    plan = _Phase2MockModelGateway._orchestrator_plan(
        {"message": "Lập kế hoạch dự án và đề xuất đội ngũ", "specialist_catalog": catalog},
        "vi",
    )
    steps = cast(list[dict[str, object]], plan["steps"])

    assert [step["capability"] for step in steps] == [
        "planning.create",
        "assignment.recommend_team",
    ]
    assert steps[1]["depends_on"] == ["create_plan"]


def test_mock_plan_uses_seeded_skills_for_combined_team_requirement_derivation() -> None:
    tasks = cast(list[dict[str, object]], _mock_plan()["tasks"])
    labels = {label for task in tasks for label in cast(list[str], task["required_skill_labels"])}

    assert labels == {"Project Management", "Manual Testing"}
