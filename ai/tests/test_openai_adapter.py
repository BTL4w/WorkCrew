"""OpenAI adapter tests that never perform a live provider call."""

from dataclasses import replace
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel, SecretStr, ValidationError

from work_management_ai.model_gateway.contracts import ModelMessage, StructuredModelRequest
from work_management_ai.model_gateway.errors import (
    ModelInvalidOutputError,
    ModelRateLimitError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from work_management_ai.model_gateway.openai import OpenAIModelGateway
from work_management_ai.schemas.planning import PlanningModelOutput

VALID_PLAN: dict[str, object] = {
    "project": {
        "title": "Product launch",
        "description": None,
        "start_date": "2026-08-10",
        "due_date": "2026-09-30",
    },
    "goal": {
        "title": "Launch on schedule",
        "description": None,
        "expected_outcomes": ["Customers can order on launch day"],
        "target_date": "2026-09-30",
    },
    "milestones": [],
    "project_weeks": [],
    "tasks": [],
    "dependencies": [],
    "assumptions": [],
}


class ProviderRateLimitError(Exception):
    status_code = 429


class FakeStructuredModel:
    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.messages: object | None = None

    async def ainvoke(self, messages: object) -> object:
        self.messages = messages
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeChatModel:
    def __init__(self, outcome: object) -> None:
        self.structured = FakeStructuredModel(outcome)
        self.schema: type[BaseModel] | None = None
        self.method: str | None = None

    def with_structured_output(
        self,
        schema: type[BaseModel],
        *,
        method: str,
        include_raw: bool = False,
    ) -> FakeStructuredModel:
        self.schema = schema
        self.method = method
        if include_raw and not isinstance(self.structured.outcome, Exception):
            self.structured.outcome = {
                "raw": AIMessage(
                    content="",
                    usage_metadata={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
                ),
                "parsed": self.structured.outcome,
                "parsing_error": None,
            }
        return self.structured


def planning_request() -> StructuredModelRequest[PlanningModelOutput]:
    return StructuredModelRequest(
        invocation_key="planning.default.en.v1",
        messages=(ModelMessage(role="user", content="Plan a product launch"),),
        output_schema=PlanningModelOutput,
        timeout_seconds=60,
    )


def gateway_with_outcome(
    outcome: object,
) -> tuple[OpenAIModelGateway, FakeChatModel, dict[str, object]]:
    chat_model = FakeChatModel(outcome)
    configuration: dict[str, object] = {}

    def factory(**kwargs: Any) -> FakeChatModel:
        configuration.update(kwargs)
        return chat_model

    gateway = OpenAIModelGateway(
        model_name="gpt-planning",
        api_key=SecretStr("test-key-not-a-real-credential"),
        chat_model_factory=factory,
    )
    return gateway, chat_model, configuration


@pytest.mark.asyncio
async def test_openai_adapter_uses_function_calling_for_flexible_typed_output() -> None:
    gateway, chat_model, configuration = gateway_with_outcome(VALID_PLAN)

    response = await gateway.generate_structured(planning_request())

    assert isinstance(response.parsed, PlanningModelOutput)
    assert response.parsed.goal.expected_outcomes == ["Customers can order on launch day"]
    assert response.model_ref == "openai:gpt-planning"
    assert configuration == {
        "model_name": "gpt-planning",
        "api_key": SecretStr("test-key-not-a-real-credential"),
        "timeout_seconds": 60,
        "use_responses_api": True,
    }
    assert chat_model.schema is PlanningModelOutput
    assert chat_model.method == "function_calling"
    assert chat_model.structured.messages == [("user", "Plan a product launch")]


@pytest.mark.asyncio
async def test_openai_adapter_forwards_explicit_output_budget() -> None:
    gateway, _, configuration = gateway_with_outcome(VALID_PLAN)
    await gateway.generate_structured(replace(planning_request(), max_output_tokens=80))
    assert configuration["max_output_tokens"] == 80


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_error", "expected_error"),
    [
        (TimeoutError("timed out"), ModelTimeoutError),
        (ConnectionError("offline"), ModelUnavailableError),
        (ProviderRateLimitError("too many requests"), ModelRateLimitError),
    ],
)
async def test_openai_adapter_normalizes_provider_errors(
    provider_error: Exception,
    expected_error: type[Exception],
) -> None:
    gateway, _, _ = gateway_with_outcome(provider_error)

    with pytest.raises(expected_error):
        await gateway.generate_structured(planning_request())


@pytest.mark.asyncio
async def test_openai_adapter_rejects_invalid_structured_output() -> None:
    gateway, _, _ = gateway_with_outcome({"project": {}})

    with pytest.raises(ModelInvalidOutputError):
        await gateway.generate_structured(planning_request())


@pytest.mark.asyncio
async def test_openai_adapter_normalizes_langchain_schema_validation_failure() -> None:
    with pytest.raises(ValidationError) as captured:
        PlanningModelOutput.model_validate({"project": {}})
    gateway, _, _ = gateway_with_outcome(captured.value)

    with pytest.raises(ModelInvalidOutputError):
        await gateway.generate_structured(planning_request())
