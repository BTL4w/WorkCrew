"""Image and usage contracts exercised without live providers or private data."""

import asyncio
import base64
import json
import logging
from dataclasses import FrozenInstanceError, replace
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from langsmith.run_helpers import get_tracing_context
from pydantic import BaseModel, SecretStr

from work_management_ai.model_gateway.contracts import (
    ModelImagePart,
    ModelMessage,
    ModelRequestLimits,
    ModelUsage,
    StructuredModelRequest,
)
from work_management_ai.model_gateway.errors import (
    ModelInvalidInputError,
    ModelInvalidOutputError,
    ModelTimeoutError,
)
from work_management_ai.model_gateway.mock import MockModelGateway
from work_management_ai.model_gateway.openai import OpenAIModelGateway


class EvidenceReading(BaseModel):
    summary: str


VALID_OUTPUT: dict[str, object] = {"summary": "The evidence shows the reported work"}


class FakeStructuredModel:
    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.messages: object = None

    async def ainvoke(self, messages: object) -> object:
        self.messages = messages
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return {
            "raw": AIMessage(
                content="",
                usage_metadata={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
            ),
            "parsed": self.outcome,
            "parsing_error": None,
        }


class FakeChatModel:
    def __init__(self, outcome: object) -> None:
        self.structured = FakeStructuredModel(outcome)

    def with_structured_output(
        self, schema: type[BaseModel], *, method: str, include_raw: bool
    ) -> FakeStructuredModel:
        return self.structured


def text_request() -> StructuredModelRequest[EvidenceReading]:
    return StructuredModelRequest(
        "evidence.demo", (ModelMessage("user", "Read evidence"),), EvidenceReading, 60
    )


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
)


def media_request() -> StructuredModelRequest[EvidenceReading]:
    return replace(
        text_request(),
        messages=(
            ModelMessage(
                "user",
                "Read this evidence",
                (ModelImagePart("image/png", PNG, "evidence:demo:v1"),),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_media_mapping_preserves_text_only_contract() -> None:
    text_only_request = text_request()
    request = media_request()
    response = await MockModelGateway(
        fixtures={request.invocation_key: VALID_OUTPUT}
    ).generate_structured(request)
    assert text_only_request.messages[0].images == ()
    assert request.messages[0].images[0].mime_type == "image/png"
    assert request.messages[0].images[0].source_ref == "evidence:demo:v1"
    assert response.parsed == EvidenceReading.model_validate(VALID_OUTPUT)
    assert response.usage is None
    with pytest.raises(FrozenInstanceError):
        request.messages[0].images[0].source_ref = "changed"  # pyright: ignore[reportAttributeAccessIssue]
    assert repr(PNG) not in repr(request)


@pytest.mark.asyncio
async def test_openai_maps_images_and_returns_only_safe_usage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    chat = FakeChatModel(VALID_OUTPUT)
    configuration: dict[str, object] = {}

    def factory(**kwargs: Any) -> FakeChatModel:
        configuration.update(kwargs)
        return chat

    gateway = OpenAIModelGateway(
        model_name="vision-test", api_key=SecretStr("test"), chat_model_factory=factory
    )
    request = media_request()
    response = await gateway.generate_structured(request)
    assert chat.structured.messages == [
        (
            "user",
            [
                {"type": "text", "text": "Read this evidence"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")
                    },
                },
            ],
        )
    ]
    assert configuration["max_output_tokens"] == 4000
    assert response.parsed == EvidenceReading.model_validate(VALID_OUTPUT)
    assert response.usage == ModelUsage(input_tokens=20, output_tokens=10)
    assert response.usage is not None
    assert response.usage.total_tokens == 30
    assert "data:image" not in repr(response)
    assert base64.b64encode(PNG).decode("ascii") not in caplog.text


@pytest.mark.parametrize(
    ("mime", "data"),
    [
        ("image/gif", PNG),
        ("image/png", "https://example.test/private.png"),
        ("image/png", b"https://example.test/private.png"),
        ("image/jpeg", PNG),
        ("image/png", b""),
    ],
)
def test_unsupported_or_url_media_is_rejected(mime: Any, data: Any) -> None:
    with pytest.raises(ValueError):
        ModelImagePart(mime, data, "evidence:demo:v1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limits",
    [
        ModelRequestLimits(max_images=0),
        ModelRequestLimits(max_image_bytes=10),
        ModelRequestLimits(max_total_image_bytes=10),
        ModelRequestLimits(max_text_bytes=5),
    ],
)
async def test_input_limits_reject_before_provider_creation(limits: ModelRequestLimits) -> None:
    calls: list[object] = []

    def factory(**kwargs: Any) -> FakeChatModel:
        calls.append(kwargs)
        return FakeChatModel(VALID_OUTPUT)

    request = replace(media_request(), limits=limits)
    gateway = OpenAIModelGateway(
        model_name="test", api_key=SecretStr("test"), chat_model_factory=factory
    )
    with pytest.raises(ModelInvalidInputError):
        await gateway.generate_structured(request)
    assert calls == []
    with pytest.raises(ModelInvalidInputError):
        await MockModelGateway(fixtures={request.invocation_key: VALID_OUTPUT}).generate_structured(
            request
        )


@pytest.mark.asyncio
async def test_explicit_output_budget_is_honored_and_over_budget_denied() -> None:
    configuration: dict[str, object] = {}

    def factory(**kwargs: Any) -> FakeChatModel:
        configuration.update(kwargs)
        return FakeChatModel(VALID_OUTPUT)

    gateway = OpenAIModelGateway(
        model_name="test", api_key=SecretStr("test"), chat_model_factory=factory
    )
    await gateway.generate_structured(replace(media_request(), max_output_tokens=80))
    assert configuration["max_output_tokens"] == 80
    for value in (0, -1, 4001):
        with pytest.raises(ModelInvalidInputError):
            await gateway.generate_structured(replace(media_request(), max_output_tokens=value))


@pytest.mark.asyncio
async def test_mock_can_return_deterministic_usage_without_images_in_response() -> None:
    request = media_request()
    gateway = MockModelGateway(
        fixtures={request.invocation_key: VALID_OUTPUT},
        usage_fixtures={request.invocation_key: ModelUsage(200, 80)},
    )
    response = await gateway.generate_structured(request)
    assert response.usage == ModelUsage(200, 80)
    assert "images" not in repr(response)


@pytest.mark.asyncio
async def test_mock_failed_output_retains_configured_usage() -> None:
    request = media_request()
    gateway = MockModelGateway(
        fixtures={request.invocation_key: {"wrong": "shape"}},
        usage_fixtures={request.invocation_key: ModelUsage(200, 80)},
    )
    with pytest.raises(ModelInvalidOutputError) as captured:
        await gateway.generate_structured(request)
    assert captured.value.usage == ModelUsage(200, 80)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [(TimeoutError("timeout"), ModelTimeoutError), ({"project": {}}, ModelInvalidOutputError)],
)
async def test_media_timeout_and_invalid_output_preserve_gateway_errors(
    outcome: object, expected: type[Exception]
) -> None:
    def factory(**_: Any) -> FakeChatModel:
        return FakeChatModel(outcome)

    gateway = OpenAIModelGateway(
        model_name="test",
        api_key=SecretStr("test"),
        chat_model_factory=factory,
    )
    with pytest.raises(expected):
        await gateway.generate_structured(media_request())


@pytest.mark.asyncio
async def test_media_disables_raw_sdk_traces_even_when_global_tracing_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    seen: list[bool | None] = []
    chat = FakeChatModel(VALID_OUTPUT)

    async def invoke(messages: object) -> object:
        seen.append(get_tracing_context()["enabled"])
        await asyncio.sleep(0)
        return {"raw": AIMessage(content=""), "parsed": VALID_OUTPUT, "parsing_error": None}

    chat.structured.ainvoke = invoke

    def factory(**_: Any) -> FakeChatModel:
        return chat

    gateway = OpenAIModelGateway(
        model_name="test", api_key=SecretStr("test"), chat_model_factory=factory
    )
    await gateway.generate_structured(media_request())
    assert seen == [False]


@pytest.mark.asyncio
@pytest.mark.parametrize("parsed", [None, {"project": {}}])
async def test_failed_output_preserves_usage_without_exposing_raw_response(parsed: object) -> None:
    chat = FakeChatModel(VALID_OUTPUT)

    async def invoke(messages: object) -> object:
        return {
            "raw": AIMessage(
                content="private raw provider output",
                usage_metadata={"input_tokens": 30, "output_tokens": 15, "total_tokens": 45},
            ),
            "parsed": parsed,
            "parsing_error": ValueError("private parser detail") if parsed is None else None,
        }

    chat.structured.ainvoke = invoke

    def factory(**_: Any) -> FakeChatModel:
        return chat

    gateway = OpenAIModelGateway(
        model_name="test", api_key=SecretStr("test"), chat_model_factory=factory
    )
    with pytest.raises(ModelInvalidOutputError) as captured:
        await gateway.generate_structured(media_request())
    assert captured.value.usage == ModelUsage(30, 15)
    assert "private" not in str(captured.value)
    assert captured.value.__suppress_context__


@pytest.mark.asyncio
async def test_sdk_transport_maps_image_and_usage_without_network(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="openai._base_client")
    payloads: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp_demo",
                "object": "response",
                "created_at": 1,
                "model": "vision-test",
                "status": "completed",
                "error": None,
                "incomplete_details": None,
                "output": [
                    {
                        "id": "fc_demo",
                        "type": "function_call",
                        "call_id": "call_demo",
                        "name": "EvidenceReading",
                        "arguments": json.dumps(VALID_OUTPUT),
                        "status": "completed",
                    }
                ],
                "usage": {
                    "input_tokens": 31,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 15,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 46,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:

        def factory(**options: Any) -> Any:
            return ChatOpenAI(
                model="vision-test",
                api_key=SecretStr("fake-key"),
                use_responses_api=True,
                max_retries=0,
                http_async_client=client,
                max_completion_tokens=options["max_output_tokens"],
            )

        gateway = OpenAIModelGateway(
            model_name="vision-test", api_key=SecretStr("fake-key"), chat_model_factory=factory
        )
        result = await gateway.generate_structured(media_request())
    assert result.parsed == EvidenceReading.model_validate(VALID_OUTPUT)
    assert result.usage == ModelUsage(31, 15)
    assert len(payloads) == 1
    assert payloads[0]["max_output_tokens"] == 4000
    assert payloads[0]["input"] == [
        {
            "role": "user",
            "type": "message",
            "content": [
                {"type": "input_text", "text": "Read this evidence"},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64," + base64.b64encode(PNG).decode("ascii"),
                },
            ],
        }
    ]
    assert base64.b64encode(PNG).decode("ascii") not in caplog.text
    assert "data:image" not in caplog.text
    logging.getLogger("openai._base_client").debug("Non-media logging still works")
    assert "Non-media logging still works" in caplog.text


@pytest.mark.asyncio
async def test_image_timeout_cancels_an_inflight_provider_call() -> None:
    cancelled = asyncio.Event()
    chat = FakeChatModel(VALID_OUTPUT)

    async def invoke(messages: object) -> object:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    chat.structured.ainvoke = invoke

    def factory(**_: Any) -> FakeChatModel:
        return chat

    gateway = OpenAIModelGateway(
        model_name="test", api_key=SecretStr("test"), chat_model_factory=factory
    )
    with pytest.raises(ModelTimeoutError):
        await gateway.generate_structured(replace(media_request(), timeout_seconds=0.01))
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_jpeg_and_png_at_limits_keep_message_order_and_source_identity() -> None:
    jpeg = ModelImagePart("image/jpeg", b"\xff\xd8\xff\xe0synthetic", "evidence:jpeg:v2")
    png = media_request().messages[0].images[0]
    request = replace(
        media_request(),
        messages=(
            ModelMessage("system", "Read only"),
            ModelMessage("user", "Compare", (jpeg, png)),
        ),
        limits=ModelRequestLimits(
            max_images=2, max_image_bytes=len(PNG), max_total_image_bytes=len(jpeg.data) + len(PNG)
        ),
    )
    chat = FakeChatModel(VALID_OUTPUT)

    def factory(**_: Any) -> FakeChatModel:
        return chat

    result = await OpenAIModelGateway(
        model_name="test", api_key=SecretStr("test"), chat_model_factory=factory
    ).generate_structured(request)
    assert result.parsed == EvidenceReading.model_validate(VALID_OUTPUT)
    assert chat.structured.messages == [
        ("system", "Read only"),
        (
            "user",
            [
                {"type": "text", "text": "Compare"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,/9j/4HN5bnRoZXRpYw=="},
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")
                    },
                },
            ],
        ),
    ]
    assert [part.source_ref for part in request.messages[1].images] == [
        "evidence:jpeg:v2",
        "evidence:demo:v1",
    ]


@pytest.mark.parametrize(
    "images", [[ModelImagePart("image/png", PNG, "demo")], ("https://example.test/image.png",)]
)
def test_message_rejects_mutable_or_untyped_image_parts(images: Any) -> None:
    with pytest.raises(ValueError):
        ModelMessage("user", "Read", images)


def test_images_require_user_role_and_nonempty_source() -> None:
    with pytest.raises(ValueError):
        ModelMessage("system", "Read", (ModelImagePart("image/png", PNG, "demo"),))
    with pytest.raises(ValueError):
        ModelImagePart("image/png", PNG, " ")
