"""Hosted OpenAI adapter kept behind project-owned gateway contracts."""

import asyncio
import base64
import logging
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from typing import Protocol, cast, override

from langchain_openai import ChatOpenAI
from langsmith import tracing_context  # pyright: ignore[reportUnknownVariableType]
from pydantic import BaseModel, SecretStr, ValidationError

from work_management_ai.model_gateway.contracts import (
    ModelUsage,
    StructuredModelRequest,
    StructuredModelResponse,
    validate_request,
)
from work_management_ai.model_gateway.errors import (
    ModelInvalidOutputError,
    normalize_model_error,
)

_private_media_call: ContextVar[bool] = ContextVar("model_gateway_private_media", default=False)


class _PrivateMediaLogFilter(logging.Filter):
    """Block SDK request/response dumps only within the current image call."""

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        return not _private_media_call.get()


# These SDK loggers can include full request bodies, response data or exceptions.
# ContextVar scope preserves logging for other concurrent calls and after timeout.
_media_log_filter = _PrivateMediaLogFilter()
for _logger_name in (
    "openai._base_client",
    "openai._response",
    "langchain_openai.chat_models.base",
):
    logging.getLogger(_logger_name).addFilter(_media_log_filter)


@contextmanager
def _private_image_request() -> Generator[None]:
    token = _private_media_call.set(True)
    try:
        with tracing_context(enabled=False):
            yield
    finally:
        _private_media_call.reset(token)


class _StructuredRunnable(Protocol):
    async def ainvoke(self, messages: object) -> object: ...


class _ChatModel(Protocol):
    def with_structured_output(
        self,
        schema: type[BaseModel],
        *,
        method: str,
        include_raw: bool,
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

        output_cap = validate_request(request)
        has_images = any(message.images for message in request.messages)
        try:
            generation_options = {"max_output_tokens": output_cap} if output_cap is not None else {}
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
                include_raw=True,
            )
            messages: list[tuple[str, str | list[dict[str, object]]]] = []
            for message in request.messages:
                if not message.images:
                    messages.append((message.role, message.content))
                    continue
                blocks: list[dict[str, object]] = [{"type": "text", "text": message.content}]
                blocks.extend(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{image.mime_type};base64,"
                            + base64.b64encode(image.data).decode("ascii")
                        },
                    }
                    for image in message.images
                )
                messages.append((message.role, blocks))
            # SDK tracing records runnable inputs unless disabled at the boundary.
            # Keep structured safe traces at the Harness, never export image bytes.
            with _private_image_request() if has_images else nullcontext():
                async with asyncio.timeout(request.timeout_seconds):
                    raw_output = await structured_model.ainvoke(messages)
        except ValidationError as error:
            raise ModelInvalidOutputError("model output failed schema validation") from (
                None if has_images else error
            )
        except Exception as error:
            raise normalize_model_error(error) from (None if has_images else error)

        usage: ModelUsage | None = None
        try:
            if not isinstance(raw_output, Mapping):
                raise ValueError("invalid structured response envelope")
            envelope = cast(Mapping[str, object], raw_output)
            metadata = getattr(envelope.get("raw"), "usage_metadata", None)
            if metadata is not None:
                if not isinstance(metadata, Mapping):
                    raise ValueError("invalid usage metadata")
                counts = cast(Mapping[str, object], metadata)
                if (
                    type(counts.get("input_tokens")) is not int
                    or type(counts.get("output_tokens")) is not int
                ):
                    raise ValueError("invalid usage metadata")
                usage = ModelUsage(
                    cast(int, counts["input_tokens"]), cast(int, counts["output_tokens"])
                )
            if envelope.get("parsing_error") is not None or envelope.get("parsed") is None:
                raise ValueError("invalid structured response")
            parsed = request.output_schema.model_validate(envelope["parsed"])
        except (TypeError, ValidationError, ValueError) as error:
            raise ModelInvalidOutputError(
                "model output failed schema validation", usage=usage
            ) from (None if has_images else error)

        return StructuredModelResponse(
            parsed=parsed,
            model_ref=f"openai:{self._model_name}",
            usage=usage,
        )
