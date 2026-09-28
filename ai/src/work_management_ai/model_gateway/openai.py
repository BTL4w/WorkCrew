"""Hosted OpenAI adapter kept behind project-owned gateway contracts."""

import asyncio
from collections.abc import Callable
from typing import Protocol, cast

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, SecretStr, ValidationError

from work_management_ai.model_gateway.contracts import (
    StructuredModelRequest,
    StructuredModelResponse,
)
from work_management_ai.model_gateway.errors import (
    ModelInvalidOutputError,
    normalize_model_error,
)


class _StructuredRunnable(Protocol):
    async def ainvoke(self, messages: object) -> object: ...


class _ChatModel(Protocol):
    def with_structured_output(
        self,
        schema: type[BaseModel],
        *,
        method: str,
    ) -> _StructuredRunnable: ...


_ChatModelFactory = Callable[..., _ChatModel]


def _create_chat_model(
    *,
    model_name: str,
    api_key: SecretStr,
    timeout_seconds: float,
    use_responses_api: bool,
    max_output_tokens: int | None = None,
) -> _ChatModel:
    """Create the external LangChain adapter without leaking it into contracts."""

    model = ChatOpenAI(
        model=model_name,
        api_key=api_key,
        timeout=timeout_seconds,
        max_retries=0,
        use_responses_api=use_responses_api,
        max_completion_tokens=max_output_tokens,
    )
    return cast(_ChatModel, model)


class OpenAIModelGateway:
    """Generate and validate typed output through hosted OpenAI models."""

    def __init__(
        self,
        *,
        model_name: str,
        api_key: SecretStr,
        chat_model_factory: _ChatModelFactory = _create_chat_model,
    ) -> None:
        self._model_name = model_name
        self._api_key = api_key
        self._chat_model_factory = chat_model_factory

    async def generate_structured[StructuredOutputT: BaseModel](
        self,
        request: StructuredModelRequest[StructuredOutputT],
    ) -> StructuredModelResponse[StructuredOutputT]:
        """Invoke OpenAI with typed output and normalize all provider failures."""

        try:
            generation_options = (
                {"max_output_tokens": request.max_output_tokens}
                if request.max_output_tokens is not None
                else {}
            )
            chat_model = self._chat_model_factory(
                model_name=self._model_name,
                api_key=self._api_key,
                timeout_seconds=request.timeout_seconds,
                use_responses_api=True,
                **generation_options,
            )
            structured_model = chat_model.with_structured_output(
                request.output_schema,
                method="function_calling",
            )
            messages = [(message.role, message.content) for message in request.messages]
            async with asyncio.timeout(request.timeout_seconds):
                raw_output = await structured_model.ainvoke(messages)
        except ValidationError as error:
            raise ModelInvalidOutputError("model output failed schema validation") from error
        except Exception as error:
            raise normalize_model_error(error) from error

        try:
            parsed = request.output_schema.model_validate(raw_output)
        except (TypeError, ValidationError, ValueError) as error:
            raise ModelInvalidOutputError("model output failed schema validation") from error

        return StructuredModelResponse(
            parsed=parsed,
            model_ref=f"openai:{self._model_name}",
        )
