from typing import Literal
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.modules.assistant.adapters.agent_runtime import build_agent_registry
from app.modules.planning_runs.adapters.ai_runtime import build_model_gateway
from work_management_ai.agents.orchestrator.contracts import (
    ActiveConversationContext,
    ExecutionPlan,
    OrchestratorInput,
)
from work_management_ai.agents.orchestrator.prompts import build_plan_messages
from work_management_ai.model_gateway.contracts import StructuredModelRequest
from work_management_ai.runtime.activation import ActivationContext
from work_management_ai.runtime.contracts import ActorReference


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("locale", "message"),
    [("en", "Search public web for public planning"), ("vi", "Tìm trên web về public planning")],
)
async def test_default_mock_supports_real_public_route(locale: Literal["vi", "en"], message: str):
    activation = ActivationContext(5, True, frozenset({"I3"}), True)
    registry, _ = build_agent_registry(activation=activation)
    value = OrchestratorInput(
        conversation_id=uuid4(),
        turn_id=uuid4(),
        locale=locale,
        message=message,
        actor=ActorReference(organization_id=uuid4(), membership_id=uuid4()),
        active_context=ActiveConversationContext(recent_messages=()),
    )
    gateway = build_model_gateway(Settings(environment="test", ai_provider="mock"))
    request = StructuredModelRequest(
        invocation_key=f"orchestrator.{locale}.plan",
        messages=build_plan_messages(
            value,
            mode="plan",
            requested_handoff=None,
            prior_plan=None,
            specialist_catalog=registry.planning_catalog(
                active_phase=5, role="EMPLOYEE", activation=activation
            ),
        ),
        output_schema=ExecutionPlan,
        timeout_seconds=25,
    )
    result = await gateway.generate_structured(request)
    assert result.parsed.steps[0].target_agent_id.value == "retrieve"
    assert result.parsed.steps[0].typed_input["search_queries"] == ["public planning"]
