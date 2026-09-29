"""Project-owned contracts for structured model generation."""

import json
import math
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel

from work_management_ai.model_gateway.errors import ModelInvalidInputError


@dataclass(frozen=True, slots=True)
class ModelImagePart:
    """Authorized image bytes and caller-owned immutable source provenance."""

    mime_type: Literal["image/jpeg", "image/png"]
    data: bytes = field(repr=False)
    source_ref: str

    def __post_init__(self) -> None:
        signatures = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff"}
        signature = signatures.get(self.mime_type)
        if (
            signature is None
            or type(self.data) is not bytes
            or not self.data.startswith(signature)
            or type(self.source_ref) is not str
            or not self.source_ref.strip()
        ):
            raise ValueError("image requires JPEG/PNG bytes and a source reference")


@dataclass(frozen=True, slots=True)
class ModelRequestLimits:
    """Transport limits, separate from model-specific token estimates.

    Applied by default to image requests; text callers may opt in. The text
    ceiling includes the output schema. Image decoding/pixel limits belong to
    the authorized evidence reader, before it constructs these parts.
    """

    max_text_bytes: int = 96_000
    max_images: int = 5
    max_image_bytes: int = 20 * 1024 * 1024
    max_total_image_bytes: int = 100 * 1024 * 1024
    max_output_tokens: int = 4000

    def __post_init__(self) -> None:
        values = (
            self.max_text_bytes,
            self.max_images,
            self.max_image_bytes,
            self.max_total_image_bytes,
        )
        if any(type(value) is not int or value < 0 for value in values) or (
            type(self.max_output_tokens) is not int or self.max_output_tokens <= 0
        ):
            raise ValueError("model limits must be non-negative; output limit must be positive")


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """Provider-reported token counts, never an estimate or raw response."""

    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 0 for value in (self.input_tokens, self.output_tokens)
        ):
            raise ValueError("token usage must contain non-negative integers")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ModelMessage:
    """A provider-neutral message passed to a model."""

    role: Literal["system", "user", "assistant"]
    content: str
    images: tuple[ModelImagePart, ...] = ()

    def __post_init__(self) -> None:
        if type(self.images) is not tuple or any(
            type(image) is not ModelImagePart for image in self.images
        ):
            raise ValueError("images must be an immutable tuple of image parts")
        if self.images and self.role != "user":
            raise ValueError("only user messages may contain input images")


@dataclass(frozen=True, slots=True)
class StructuredModelRequest[StructuredOutputT: BaseModel]:
    """A typed structured-output request independent of provider SDKs."""

    invocation_key: str
    messages: tuple[ModelMessage, ...]
    output_schema: type[StructuredOutputT]
    timeout_seconds: float
    max_output_tokens: int | None = None
    limits: ModelRequestLimits | None = None


@dataclass(frozen=True, slots=True)
class StructuredModelResponse[StructuredOutputT: BaseModel]:
    """Validated structured output plus stable model-version metadata."""

    parsed: StructuredOutputT
    model_ref: str
    usage: ModelUsage | None = None


def validate_request[StructuredOutputT: BaseModel](
    request: StructuredModelRequest[StructuredOutputT],
) -> int | None:
    """Reject oversized input before provider creation; return its output cap."""

    images = tuple(image for message in request.messages for image in message.images)
    limits = request.limits or (ModelRequestLimits() if images else None)
    if not math.isfinite(request.timeout_seconds) or request.timeout_seconds <= 0:
        raise ModelInvalidInputError("model timeout must be finite and positive")
    cap = request.max_output_tokens
    if cap is not None and (type(cap) is not int or cap <= 0):
        raise ModelInvalidInputError("model output limit must be positive")
    if limits is None:
        return cap
    text_bytes = sum(len(message.content.encode("utf-8")) for message in request.messages)
    text_bytes += len(json.dumps(request.output_schema.model_json_schema()).encode("utf-8"))
    if (
        text_bytes > limits.max_text_bytes
        or len(images) > limits.max_images
        or any(len(image.data) > limits.max_image_bytes for image in images)
        or sum(len(image.data) for image in images) > limits.max_total_image_bytes
        or (cap is not None and cap > limits.max_output_tokens)
    ):
        raise ModelInvalidInputError("model request exceeds configured limits")
    return cap if cap is not None else limits.max_output_tokens


class ModelGateway(Protocol):
    """Port implemented by deterministic and hosted model providers."""

    async def generate_structured[StructuredOutputT: BaseModel](
        self,
        request: StructuredModelRequest[StructuredOutputT],
    ) -> StructuredModelResponse[StructuredOutputT]:
        """Return output validated against the request schema."""

        ...
