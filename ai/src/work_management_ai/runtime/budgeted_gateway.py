"""Charge conservative reservations before entering the provider boundary."""

import asyncio
import json

from pydantic import BaseModel

from work_management_ai.model_gateway.contracts import (
    ModelGateway,
    StructuredModelRequest,
    StructuredModelResponse,
)
from work_management_ai.model_gateway.errors import ModelRateLimitError, ModelTimeoutError
from work_management_ai.runtime.daily_update_budget import (
    DAILY_MODEL_SCOPE,
    MODEL_ATTEMPT_SCOPE,
    UsageLimitExceeded,
    UsageStore,
)


class BudgetedDailyGateway:
    def __init__(
        self, gateway: ModelGateway, store: UsageStore, *, image_token_bound: int | None = None
    ):
        self.gateway = gateway
        self.store = store
        self.image_token_bound = image_token_bound

    async def generate_structured[OutputT: BaseModel](
        self, request: StructuredModelRequest[OutputT]
    ) -> StructuredModelResponse[OutputT]:
        scope = DAILY_MODEL_SCOPE.get()
        if scope is None:
            raise UsageLimitExceeded("DAILY_UPDATE_BUDGET_SCOPE_REQUIRED")
        images = sum(len(m.images) for m in request.messages)
        if images and self.image_token_bound is None:
            raise UsageLimitExceeded("MEDIA_TOKEN_BOUND_UNAVAILABLE")
        # UTF-8 bytes conservatively bound text tokens for supported byte-level tokenizers;
        # schema plus explicit framing allowance are included. Media has a separate bound.
        inputs = sum(len(m.content.encode()) for m in request.messages)
        inputs += len(json.dumps(request.output_schema.model_json_schema()).encode()) + 1024
        inputs += images * (self.image_token_bound or 0)
        outputs = request.max_output_tokens or 4000
        for attempt in range(2):
            charged_attempt = await self.store.reserve(
                scope, input_tokens=inputs, output_tokens=outputs
            )
            remaining = await self.store.remaining_seconds(scope)
            if remaining <= 0:
                raise UsageLimitExceeded("DAILY_UPDATE_MODEL_BUDGET_EXHAUSTED")
            token = MODEL_ATTEMPT_SCOPE.set(charged_attempt)
            try:
                async with asyncio.timeout(min(60, request.timeout_seconds, remaining)):
                    response = await self.gateway.generate_structured(request)
                break
            except (ModelTimeoutError, ModelRateLimitError, TimeoutError):
                if attempt == 1:
                    raise
            finally:
                MODEL_ATTEMPT_SCOPE.reset(token)
        else:
            raise UsageLimitExceeded("MODEL_ATTEMPTS_EXHAUSTED")
        if response.usage and (
            response.usage.input_tokens > inputs or response.usage.output_tokens > outputs
        ):
            raise UsageLimitExceeded("PROVIDER_USAGE_EXCEEDS_RESERVATION")
        return response
