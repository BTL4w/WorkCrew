"""Bilingual naming, input budgets and safe fallback without live calls."""

import asyncio
from typing import Literal

import pytest
from pydantic import BaseModel

from work_management_ai.conversation_title import generate_title
from work_management_ai.model_gateway.contracts import (
    StructuredModelRequest,
    StructuredModelResponse,
)
from work_management_ai.model_gateway.mock import MockModelGateway


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("locale", "message", "title"),
    [
        ("vi", "Lập kế hoạch ra mắt sản phẩm", "Kế hoạch ra mắt sản phẩm"),
        ("en", "Plan a customer launch event", "Customer launch event planning"),
    ],
)
async def test_first_message_produces_validated_bilingual_title(
    locale: Literal["vi", "en"], message: str, title: str
) -> None:
    gateway = MockModelGateway(
        fixtures={f"conversation_title.{locale}.v1": {"title": title}},
        model_ref="mock:title-v1",
    )
    result = await generate_title(gateway, message=message, locale=locale)
    assert result.title == title
    assert result.model_ref == "mock:title-v1"
    assert not result.fallback


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [{}, {"title": "   "}, {"title": "x" * 101}, TimeoutError()])
async def test_invalid_output_and_timeout_use_first_message_fallback(output: object) -> None:
    gateway = MockModelGateway(fixtures={"conversation_title.en.v1": output})
    result = await generate_title(
        gateway, message="Plan a customer launch event for next month with the team", locale="en"
    )
    assert result.title == "Plan a customer launch event for"
    assert result.fallback
    assert result.safe_error_code is not None


@pytest.mark.asyncio
async def test_request_sends_only_bounded_first_message_with_output_budget() -> None:
    class Gateway:
        async def generate_structured[OutputT: BaseModel](
            self, request: StructuredModelRequest[OutputT]
        ) -> StructuredModelResponse[OutputT]:
            assert len(request.messages) == 2
            assert request.messages[1].content == "a" * 2000
            assert request.max_output_tokens == 80
            return StructuredModelResponse(
                parsed=request.output_schema.model_validate({"title": "A short title"}),
                model_ref="mock:title-v1",
            )

    result = await generate_title(Gateway(), message="a" * 5000, locale="en")
    assert result.title == "A short title"


@pytest.mark.asyncio
async def test_slow_gateway_is_bounded_even_if_it_ignores_request_timeout() -> None:
    class Gateway:
        async def generate_structured[OutputT: BaseModel](
            self, request: StructuredModelRequest[OutputT]
        ) -> StructuredModelResponse[OutputT]:
            await asyncio.sleep(10)
            raise AssertionError("should have been cancelled")

    result = await generate_title(
        Gateway(), message="Plan my work", locale="en", timeout_seconds=0.01
    )
    assert result.title == "Plan my work"
    assert result.fallback
