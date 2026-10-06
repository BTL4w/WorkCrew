"""Activation and permissions enforced by real registries and policy guard."""

from uuid import uuid4

import pytest

from work_management_ai.observability.safe_trace import SafeTraceRecord
from work_management_ai.runtime.agent_registry import AgentRegistry, AgentRegistryError
from work_management_ai.runtime.contracts import AgentBudget, AgentId
from work_management_ai.runtime.manifests import (
    SkillManifest,
    ToolManifest,
    load_yaml_resource,
)
from work_management_ai.runtime.skill_registry import SkillRegistry
from work_management_ai.runtime.tool_registry import ToolRegistry


def test_reporting_manifest_and_bounds():
    skills = SkillRegistry(
        load_yaml_resource(f"work_management_ai.skills.{name}", "skill.yaml", SkillManifest)
        for name in ("summarize_verified_project_metrics", "draft_management_report")
    )
    tools = ToolRegistry(
        load_yaml_resource("work_management_ai.tools.reporting", resource, ToolManifest)
        for resource in ("tool.yaml", "propose.yaml", "chat.yaml")
    )
    registry = AgentRegistry(
        skill_registry=skills,
        tool_registry=tools,
        evaluator_ids=frozenset({"reporting_numeric@1", "reporting_grounding@1"}),
    )
    registry.register_resource("work_management_ai.agents.reporting", "agent.yaml")
    with pytest.raises(AgentRegistryError, match="PHASE_INACTIVE"):
        registry.resolve(AgentId.REPORTING, "1.0.0", 4)
    assert not registry.planning_catalog(active_phase=5, role="EMPLOYEE")
    assert registry.planning_catalog(active_phase=5, role="MANAGER")[0]["agent_id"] == "reporting"
    manifest = registry.resolve(AgentId.REPORTING, "1.0.0", 5).manifest
    assert set(manifest.allowed_tools) == {
        "reporting.read@1",
        "reporting.propose@1",
        "reporting.chat@1",
    }
    default = AgentBudget(max_iterations=1, max_tool_calls=0, timeout_seconds=1)
    assert (default.max_input_tokens, default.max_output_tokens) == (24000, 4000)


@pytest.mark.parametrize("agent", ["assignment", "daily_update", "risk", "reporting"])
def test_active_agents_have_metadata_only_safe_traces(agent: str):
    record = SafeTraceRecord.model_validate(
        dict(
            orchestration_run_id=uuid4(),
            agent_id=agent,
            agent_version="1.0.0",
            workflow_version="v1",
            prompt_version="v1",
            verifier_versions=[],
            status="FAILED",
            iteration_count=0,
            tool_call_count=0,
            handoff_count=0,
            duration_ms=0,
        )
    )
    assert record.agent_id == agent
    with pytest.raises(ValueError):
        SafeTraceRecord.model_validate({**record.model_dump(), "prompt": "private"})
