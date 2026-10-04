"""Only the hub may turn a verified Risk request into a Manager Planning proposal."""

from typing import Literal
from uuid import uuid4

import pytest

from work_management_ai.agents.orchestrator.contracts import (
    ActiveConversationContext,
    OrchestratorInput,
)
from work_management_ai.agents.orchestrator.harness import OrchestratorHarness
from work_management_ai.agents.risk.harness import RiskHarness
from work_management_ai.agents.risk.tests.test_harness import Actors, Tools
from work_management_ai.model_gateway.mock import MockModelGateway
from work_management_ai.runtime.agent_registry import AgentRegistry
from work_management_ai.runtime.contracts import (
    ActorReference,
    AgentHandoff,
    AgentId,
    AgentResult,
    AgentRunStatus,
    ResolvedActorContext,
)
from work_management_ai.runtime.manifests import SkillManifest, ToolManifest, load_yaml_resource
from work_management_ai.runtime.policy_guard import PolicyGuard
from work_management_ai.runtime.skill_registry import SkillRegistry
from work_management_ai.runtime.tool_registry import ToolRegistry


def registry() -> AgentRegistry:
    value = AgentRegistry(
        skill_registry=SkillRegistry(
            load_yaml_resource(f"work_management_ai.skills.{name}", "skill.yaml", SkillManifest)
            for name in (
                "create_project_plan",
                "revise_project_plan",
                "explain_verified_risk",
                "review_evidence_concerns",
            )
        ),
        tool_registry=ToolRegistry(
            load_yaml_resource(f"work_management_ai.tools.{name}", "tool.yaml", ToolManifest)
            for name in ("risk", "planning.manage_run")
        ),
        evaluator_ids=frozenset(
            {
                "orchestrator_plan@1",
                "planning_schema@1",
                "planning_invariants@1",
                "planning_grounding@1",
                "risk_grounding@1",
            }
        ),
    )
    for agent in ("orchestrator", "planning", "risk"):
        value.register_resource(f"work_management_ai.agents.{agent}", "agent.yaml")
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["vi", "en"])
@pytest.mark.parametrize("scenario", ["ready", "employee", "stale", "unavailable", "read_only"])
async def test_verified_risk_replan_is_bound_by_hub_to_planning(
    locale: Literal["vi", "en"], scenario: str
):
    tools = Tools()
    if scenario == "stale":
        tools.changed = True
    if scenario == "unavailable":
        tools.snapshot.update(state="UNAVAILABLE", score=None, band=None)

    class CurrentActors(Actors):
        async def resolve(self, reference: ActorReference) -> ResolvedActorContext:
            return ResolvedActorContext(
                **reference.model_dump(),
                role="EMPLOYEE" if scenario == "employee" else "MANAGER",
                is_active=True,
            )

    calls: list[AgentHandoff] = []
    risk = RiskHarness(
        model_gateway=MockModelGateway(
            fixtures={
                f"risk.{locale}.explain": {
                    "observation_explanations": [
                        {
                            "text": "Review recorded status",
                            "observation_ids": ["observation:0"],
                            "source_ids": ["task:1:v2"],
                            "assertions": [
                                {
                                    "source_id": "task:1:v2",
                                    "field": "status",
                                    "value": "IN_PROGRESS",
                                }
                            ],
                        }
                    ],
                    "limitations": [],
                    "recommendations": [],
                    "replan_requested": True,
                }
            }
        ),
        tool_executor=tools,
        actor_resolver=CurrentActors(),
    )

    class Specialists:
        async def run_specialist(self, handoff: AgentHandoff) -> AgentResult:
            calls.append(handoff)
            if handoff.target_agent_id is AgentId.RISK:
                return await risk.run(handoff)
            return AgentResult(
                agent_id=AgentId.PLANNING,
                agent_version="1.0.0",
                status=AgentRunStatus.AWAITING_HUMAN,
                typed_output={
                    "workflow_run_id": str(uuid4()),
                    "operation": "REVISE",
                    "workflow_status": "QUEUED",
                    "proposal_id": None,
                    "proposal_version": None,
                    "approval_id": None,
                    "awaiting": "MANAGER_DECISION",
                    "public_summary": "Queued",
                },
                stop_reason="HUMAN_GATE",
            )

    plan: dict[str, object] = {
        "objectives": ["Replan risk"],
        "steps": [
            {
                "step_id": "risk",
                "target_agent_id": "risk",
                "target_agent_version": "1.0.0",
                "capability": "risk.explain",
                "objective": "Explain risk",
                "typed_input": {"task_reference": str(tools.task)},
                "depends_on": [],
                "mode": "READ_ONLY",
            }
        ],
        "unavailable_capabilities": [],
        "response_language": locale,
    }
    result = await OrchestratorHarness(
        model_gateway=MockModelGateway(fixtures={f"orchestrator.{locale}.plan": plan}),
        registry=registry(),
        policy_guard=PolicyGuard(),
        actor_resolver=CurrentActors(),
        specialists=Specialists(),
    ).run_turn(
        OrchestratorInput(
            conversation_id=uuid4(),
            turn_id=uuid4(),
            message=(
                "Explain this risk"
                if scenario == "read_only"
                else "Điều chỉnh kế hoạch tuần do rủi ro"
                if locale == "vi"
                else "Replan the weekly delivery due to risk"
            ),
            locale=locale,
            actor=ActorReference(organization_id=uuid4(), membership_id=uuid4()),
            active_context=ActiveConversationContext(recent_messages=()),
        )
    )
    if scenario != "ready":
        assert all(c.target_agent_id is not AgentId.PLANNING for c in calls)
        return
    assert [c.target_agent_id for c in calls] == [AgentId.RISK, AgentId.PLANNING]
    binding = calls[1].typed_input["risk_context"]
    assert isinstance(binding, dict)
    assert binding["risk_assessment_id"] == str(tools.assessment)
    assert binding["fingerprint"] == "a" * 64
    assert binding["observation_ids"] == ["observation:0"]
    assert result.status.value == "AWAITING_HUMAN"
