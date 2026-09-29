"""Normalized Model Gateway failures."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from work_management_ai.model_gateway.contracts import ModelUsage


class ModelGatewayError(RuntimeError):
    """Base class for failures exposed through the gateway contract."""


class ModelTimeoutError(ModelGatewayError):
    """The provider did not finish within the configured timeout."""


class ModelUnavailableError(ModelGatewayError):
    """The selected provider or deterministic fixture is unavailable."""


class ModelRateLimitError(ModelGatewayError):
    """The provider rejected the request because of a rate limit."""


class ModelInvalidOutputError(ModelGatewayError):
    """The provider response did not match the requested output schema."""

    def __init__(self, message: str, *, usage: ModelUsage | None = None) -> None:
        super().__init__(message)
        self.usage = usage


class ModelInvalidInputError(ModelGatewayError):
    """The request violates a deterministic input or output budget."""


def normalize_model_error(error: Exception) -> ModelGatewayError:
    """Map provider-shaped exceptions to stable project-owned failures."""

    if isinstance(error, ModelGatewayError):
        return error
    if isinstance(error, TimeoutError):
        return ModelTimeoutError("model request timed out")
    if getattr(error, "status_code", None) == 429:
        return ModelRateLimitError("model provider rate limit exceeded")
    return ModelUnavailableError("model provider unavailable")
