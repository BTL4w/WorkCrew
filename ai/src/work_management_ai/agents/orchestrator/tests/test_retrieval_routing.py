"""Public retrieval uses one routing call and cannot expand user source scope."""

# pyright: reportPrivateUsage=false
from datetime import UTC, datetime
from typing import Literal

import pytest

from work_management_ai.agents.orchestrator.harness import OrchestratorHarness
from work_management_ai.agents.orchestrator.tests.test_harness import (
    RecordingSpecialistRunner,
    StaticActorResolver,
    _input,
    _resolved_actor,
)
from work_management_ai.agents.retrieve.contracts import RetrieveOutcome, RetrieveOutput
from work_management_ai.model_gateway.mock import MockModelGateway
from work_management_ai.runtime.activation import ActivationContext
from work_management_ai.runtime.contracts import (
    ActorReference,
    AgentId,
    AgentResult,
    AgentRunStatus,
    JsonValue,
)
from work_management_ai.runtime.policy_guard import PolicyGuard


def registry():
    # Combine actual core/web manifests; no fabricated capabilities.
    from work_management_ai.runtime.agent_registry import AgentRegistry
    from work_management_ai.runtime.manifests import SkillManifest, ToolManifest, load_yaml_resource
    from work_management_ai.runtime.skill_registry import SkillRegistry
    from work_management_ai.runtime.tool_registry import ToolRegistry

    tools = ToolRegistry(
        load_yaml_resource("work_management_ai.tools.web", name, ToolManifest)
        for name in ("search.yaml", "read_pages.yaml", "revalidate.yaml")
    )
    skills = SkillRegistry(
        [
            load_yaml_resource(
                "work_management_ai.skills.web_search_and_summarize", "skill.yaml", SkillManifest
            )
        ]
    )
    result = AgentRegistry(
        skill_registry=skills,
        tool_registry=tools,
        evaluator_ids=frozenset({"retrieve_grounding@1"}),
    )
    result.register_resource("work_management_ai.agents.retrieve", "agent.yaml")
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("locale", "message"),
    [("en", "Search public web for public planning"), ("vi", "Tìm trên web về public planning")],
)
async def test_public_answer_has_one_route_call_and_no_rewrite(
    locale: Literal["vi", "en"], message: str
):
    actor = _resolved_actor("EMPLOYEE")
    output = RetrieveOutput(
        outcome=RetrieveOutcome.NOT_FOUND, strategy_used="WEB", observed_at=datetime.now(UTC)
    )
    runner = RecordingSpecialistRunner(
        {
            "retrieval.search_web": [
                AgentResult(
                    agent_id=AgentId.RETRIEVE,
                    agent_version="1.0.0",
                    status=AgentRunStatus.COMPLETED,
                    typed_output=output.model_dump(mode="json"),
                    stop_reason="NOT_FOUND",
                )
            ]
        }
    )
    start = message.index("public planning")
    intent = dict(
        search_queries=["public planning"],
        message_spans=[dict(start=start, end=start + 15, text="public planning")],
        requirements=[dict(requirement_id="topic", description="public planning")],
        source_mode="WEB",
        answer_mode="SUMMARY",
    )
    plan = dict(
        objectives=["public guidance"],
        response_language=locale,
        steps=[
            dict(
                step_id="web",
                target_agent_id="retrieve",
                target_agent_version="1.0.0",
                capability="retrieval.search_web",
                objective="public guidance",
                typed_input=intent,
                mode="READ_ONLY",
            )
        ],
    )
    gateway = MockModelGateway(fixtures={f"orchestrator.{locale}.plan": plan})
    harness = OrchestratorHarness(
        model_gateway=gateway,
        registry=registry(),
        policy_guard=PolicyGuard(),
        actor_resolver=StaticActorResolver(actor),
        specialists=runner,
        activation=ActivationContext(5, True, frozenset({"I3"}), True),
    )
    result = await harness.run_turn(_input(actor, locale=locale, message=message))
    assert result.status.value == "COMPLETED"
    assert len(runner.handoffs) == 1
    assert runner.handoffs[0].typed_input["search_queries"] == ["public planning"]
    assert "message_spans" not in runner.handoffs[0].typed_input
    assert result.blocks


@pytest.mark.asyncio
@pytest.mark.parametrize("source_mode", ["DOCUMENTS", "BOTH"])
async def test_inactive_documents_do_not_send_terms_to_web(source_mode: str):
    actor = _resolved_actor("EMPLOYEE")
    runner = RecordingSpecialistRunner()
    message = "Search my private documents"
    intent = dict(
        search_queries=["private documents"],
        message_spans=[dict(start=10, end=27, text="private documents")],
        requirements=[dict(requirement_id="topic", description="Internal brief")],
        source_mode=source_mode,
        answer_mode="SUMMARY",
    )
    plan = dict(
        objectives=["Documents"],
        response_language="en",
        steps=[
            dict(
                step_id="docs",
                target_agent_id="retrieve",
                target_agent_version="1.0.0",
                capability="retrieval.search_web",
                objective="Documents",
                typed_input=intent,
                mode="READ_ONLY",
            )
        ],
    )
    harness = OrchestratorHarness(
        model_gateway=MockModelGateway(fixtures={"orchestrator.en.plan": plan}),
        registry=registry(),
        policy_guard=PolicyGuard(),
        actor_resolver=StaticActorResolver(actor),
        specialists=runner,
        activation=ActivationContext(5, True, frozenset({"I3"}), True),
    )
    result = await harness.run_turn(_input(actor, locale="en", message=message))
    assert not runner.handoffs
    assert result.stop_reason == "CAPABILITY_UNAVAILABLE"
    assert result.blocks[0].kind == "capability_unavailable"


@pytest.mark.asyncio
async def test_application_egress_refusal_asks_without_delegation():
    actor = _resolved_actor("EMPLOYEE")
    runner = RecordingSpecialistRunner()

    class DeniedPublicScope:
        async def validate_public_intent(
            self, intent: dict[str, JsonValue], message: str, reference: ActorReference
        ) -> None:
            raise ValueError("PUBLIC_QUERY_PROTECTED_VALUE")

    message = "Search public web for Project Orion"
    intent = dict(
        search_queries=["Project Orion"],
        message_spans=[dict(start=22, end=35, text="Project Orion")],
        requirements=[dict(requirement_id="topic", description="Public guidance")],
        source_mode="WEB",
        answer_mode="SUMMARY",
    )
    plan = dict(
        objectives=["Public guidance"],
        response_language="en",
        steps=[
            dict(
                step_id="web",
                target_agent_id="retrieve",
                target_agent_version="1.0.0",
                capability="retrieval.search_web",
                objective="Public guidance",
                typed_input=intent,
                mode="READ_ONLY",
            )
        ],
    )
    harness = OrchestratorHarness(
        model_gateway=MockModelGateway(fixtures={"orchestrator.en.plan": plan}),
        registry=registry(),
        policy_guard=PolicyGuard(),
        actor_resolver=StaticActorResolver(actor),
        specialists=runner,
        activation=ActivationContext(5, True, frozenset({"I3"}), True),
        public_query_validator=DeniedPublicScope(),
    )
    result = await harness.run_turn(_input(actor, locale="en", message=message))
    assert not runner.handoffs
    assert result.status.value == "AWAITING_INPUT"
    assert result.blocks[0].kind == "text"
