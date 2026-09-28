"""Project-owned contracts for structured model generation."""

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class ModelMessage:
    """A provider-neutral message passed to a model."""

    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class StructuredModelRequest[StructuredOutputT: BaseModel]:
    """A typed structured-output request independent of provider SDKs."""

    invocation_key: str
    messages: tuple[ModelMessage, ...]
    output_schema: type[StructuredOutputT]
    timeout_seconds: float
    max_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class StructuredModelResponse[StructuredOutputT: BaseModel]:
    """Validated structured output plus stable model-version metadata."""

    parsed: StructuredOutputT
    model_ref: str


class ModelGateway(Protocol):
    """Port implemented by deterministic and hosted model providers."""

    async def generate_structured[StructuredOutputT: BaseModel](
        self,
        request: StructuredModelRequest[StructuredOutputT],
    ) -> StructuredModelResponse[StructuredOutputT]:
        """Return output validated against the request schema."""

        ...
