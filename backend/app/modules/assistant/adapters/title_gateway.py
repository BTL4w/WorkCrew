"""Dedicated cheap naming model behind the shared provider-neutral gateway."""

from pydantic import BaseModel

from app.core.config import Settings
from work_management_ai.conversation_title import fallback_title
from work_management_ai.model_gateway.contracts import (
    ModelGateway,
    StructuredModelRequest,
    StructuredModelResponse,
)
from work_management_ai.model_gateway.errors import ModelUnavailableError
from work_management_ai.model_gateway.openai import OpenAIModelGateway


class MockTitleGateway:
    async def generate_structured[OutputT: BaseModel](
        self, request: StructuredModelRequest[OutputT]
    ) -> StructuredModelResponse[OutputT]:
        return StructuredModelResponse(
            parsed=request.output_schema.model_validate(
                {
                    "title": fallback_title(request.messages[-1].content),
                }
            ),
            model_ref="mock:conversation-title-v1",
        )


class DisabledTitleGateway:
    async def generate_structured[OutputT: BaseModel](
        self, request: StructuredModelRequest[OutputT]
    ) -> StructuredModelResponse[OutputT]:
        raise ModelUnavailableError("naming provider is disabled")


def build_title_gateway(settings: Settings) -> ModelGateway:
    if settings.ai_provider == "openai" and settings.openai_api_key is not None:
        return OpenAIModelGateway(
            model_name=settings.ai_title_model,
            api_key=settings.openai_api_key,
        )
    if settings.ai_provider == "mock":
        return MockTitleGateway()
    return DisabledTitleGateway()
