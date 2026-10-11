"""Hosted quality is measured only on an explicitly enabled, credentialed model."""

import asyncio
import json
import os
from dataclasses import replace

import pytest
from pydantic import BaseModel, SecretStr

from work_management_ai.evaluation.progressive_planning import (
    load_cases,
    run_progressive_planning_suite,
)
from work_management_ai.model_gateway.contracts import (
    ModelGateway,
    StructuredModelRequest,
    StructuredModelResponse,
)
from work_management_ai.model_gateway.errors import ModelInvalidInputError
from work_management_ai.model_gateway.openai import OpenAIModelGateway


class HostedBudgetGateway:
    """Hard request/reservation caps for each explicitly opted-in repetition."""

    def __init__(self, gateway: ModelGateway) -> None:
        self.gateway = gateway
        self.calls = self.input_reserved = self.output_reserved = 0

    async def generate_structured[T: BaseModel](
        self, request: StructuredModelRequest[T]
    ) -> StructuredModelResponse[T]:
        inputs = (
            sum(len(message.content.encode()) for message in request.messages)
            + len(json.dumps(request.output_schema.model_json_schema()).encode())
            + 1024
        )
        output = min(request.max_output_tokens or 1000, 2800)
        if (
            self.calls >= 64
            or self.input_reserved + inputs > 512000
            or self.output_reserved + output > 64000
        ):
            raise ModelInvalidInputError("hosted evaluation budget exhausted")
        self.calls += 1
        self.input_reserved += inputs
        self.output_reserved += output
        return await self.gateway.generate_structured(
            replace(
                request, max_output_tokens=output, timeout_seconds=min(request.timeout_seconds, 60)
            )
        )


pytestmark = [
    pytest.mark.live_provider,
    pytest.mark.skipif(
        os.getenv("RUN_PROGRESSIVE_HOSTED_EVALUATION") != "1",
        reason="hosted progressive evaluation is opt-in",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("repetition", range(3))
async def test_hosted_bilingual_semantic_gate(repetition: int) -> None:
    model = os.getenv("APP_AI_MODEL", "")
    key = os.getenv("APP_OPENAI_API_KEY", "")
    if not model or not key:
        pytest.skip("configured main model and key are required")
    async with asyncio.timeout(600):
        report = await run_progressive_planning_suite(
            load_cases(),
            HostedBudgetGateway(OpenAIModelGateway(model_name=model, api_key=SecretStr(key))),
        )
    assert report.graph_passed and report.hosted_status == "MEASURED"
    # Six service boundaries remain NOT_RUN here; only real PostgreSQL evidence can pass them.
    assert not report.passed
