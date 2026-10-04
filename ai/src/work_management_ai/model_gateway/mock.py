"""Deterministic Model Gateway used by local development and automated tests."""

from collections.abc import Mapping
from copy import deepcopy
from types import MappingProxyType

from pydantic import BaseModel, ValidationError

from work_management_ai.model_gateway.contracts import (
    ModelUsage,
    StructuredModelRequest,
    StructuredModelResponse,
    validate_request,
)
from work_management_ai.model_gateway.errors import (
    ModelInvalidOutputError,
    ModelUnavailableError,
    normalize_model_error,
)


class MockModelGateway:
    """Select immutable fixtures deterministically by invocation key."""

    def __init__(
        self,
        *,
        fixtures: Mapping[str, object],
        model_ref: str = "mock:planning-v1",
        usage_fixtures: Mapping[str, ModelUsage] | None = None,
        sequence_fixtures: Mapping[str, tuple[object, ...]] | None = None,
    ) -> None:
        self._fixtures = MappingProxyType(deepcopy(dict(fixtures)))
        self._sequences = {
            key: list(deepcopy(values)) for key, values in (sequence_fixtures or {}).items()
        }
        self._model_ref = model_ref
        self._usage_fixtures = MappingProxyType(dict(usage_fixtures or {}))

    async def generate_structured[StructuredOutputT: BaseModel](
        self,
        request: StructuredModelRequest[StructuredOutputT],
    ) -> StructuredModelResponse[StructuredOutputT]:
        """Return a typed fixture or a normalized deterministic failure."""

        validate_request(request)
        if request.invocation_key in self._sequences:
            sequence = self._sequences[request.invocation_key]
            if not sequence:
                raise ModelUnavailableError("model fixture sequence exhausted")
            fixture = sequence.pop(0)
        elif request.invocation_key in self._fixtures:
            fixture = self._fixtures[request.invocation_key]
        else:
            raise ModelUnavailableError("model fixture unavailable")
        if isinstance(fixture, Exception):
            raise normalize_model_error(fixture) from fixture

        try:
            parsed = request.output_schema.model_validate(fixture)
        except (TypeError, ValidationError, ValueError) as error:
            raise ModelInvalidOutputError(
                "model output failed schema validation",
                usage=self._usage_fixtures.get(request.invocation_key),
            ) from error

        return StructuredModelResponse(
            parsed=parsed,
            model_ref=self._model_ref,
            usage=self._usage_fixtures.get(request.invocation_key),
        )
