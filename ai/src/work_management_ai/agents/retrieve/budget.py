"""Cumulative limits over a required durable usage/checkpoint boundary."""

from __future__ import annotations

import hashlib
import math
import re
from time import monotonic
from typing import Any, Literal, Protocol

from pydantic import Field

from work_management_ai.agents.retrieve.contracts import (
    Fingerprint,
    GroundedSummary,
    RetrievalContract,
    RetrieveLimits,
    SourceBinding,
)
from work_management_ai.model_gateway.contracts import ModelUsage
from work_management_ai.runtime.contracts import AgentHandoff, JsonValue


class RetrievalBudgetError(ValueError):
    def __init__(self, safe_code: str):
        self.safe_code = (
            safe_code
            if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", safe_code)
            else "RETRIEVAL_BUDGET_UNAVAILABLE"
        )
        super().__init__(self.safe_code)


class ModelReservation(RetrievalContract):
    reservation_id: str = Field(min_length=1, max_length=128)
    attempt_no: int = Field(ge=1, le=2, strict=True)


class SavedToolCall(RetrievalContract):
    call_id: str = Field(min_length=1, max_length=128)
    tool_id: Literal["web.search", "web.read_pages", "web.revalidate"]
    typed_input: dict[str, JsonValue]
    status: Literal["PENDING", "SUCCEEDED", "FAILED", "REJECTED"] = "PENDING"
    sources: tuple[SourceBinding, ...] = Field(default=(), max_length=8)


class RetrievalCheckpoint(RetrievalContract):
    binding: Fingerprint
    version: int = Field(default=0, ge=0, strict=True)
    completed_nodes: tuple[str, ...] = Field(default=(), max_length=12)
    nodes_used: int = Field(default=0, ge=0, le=12, strict=True)
    tool_calls: tuple[SavedToolCall, ...] = Field(default=(), max_length=8)
    queries: int = Field(default=0, ge=0, le=2, strict=True)
    page_reads: int = Field(default=0, ge=0, le=3, strict=True)
    provider_requests: int = Field(default=0, ge=0, le=8, strict=True)
    model_attempts: int = Field(default=0, ge=0, le=2, strict=True)
    input_reserved: int = Field(default=0, ge=0, le=12000, strict=True)
    output_reserved: int = Field(default=0, ge=0, le=3200, strict=True)
    observed_input_tokens: int | None = Field(default=0, ge=0)
    observed_output_tokens: int | None = Field(default=0, ge=0)
    model_state: Literal["PENDING", "IN_FLIGHT", "RETRYABLE", "COMPLETED", "FAILED"] = "PENDING"
    summary: GroundedSummary | None = None
    pack_fingerprint: Fingerprint | None = None
    source_bindings: tuple[SourceBinding, ...] = Field(default=(), max_length=8)
    limitations: tuple[str, ...] = Field(default=(), max_length=16)
    partial: bool = False
    source_failed: bool = False


class RetrievalUsagePort(Protocol):
    """Trusted scope-bound storage. Implement with Task3 services at runtime wiring.

    remaining_seconds derives from canonical persisted run/retrieval deadlines.
    Reservations are atomic/fenced; save_checkpoint must use version CAS, and
    load_checkpoint must enforce actor/tenant/retention. No production default.
    """

    async def remaining_seconds(self, *, retrieval: bool = False) -> float: ...
    async def load_checkpoint(self) -> RetrievalCheckpoint | None: ...
    async def save_checkpoint(self, checkpoint: RetrievalCheckpoint) -> None: ...
    async def reserve_node(self, operation_key: str) -> None: ...
    async def reserve_model_attempt(
        self,
        *,
        attempt_no: int,
        input_tokens: int,
        output_tokens: int,
    ) -> ModelReservation: ...
    async def record_model_usage(
        self,
        reservation: ModelReservation,
        usage: ModelUsage | None,
        *,
        safe_code: str | None = None,
    ) -> None: ...


class RetrievalBudget:
    def __init__(
        self,
        *,
        limits: RetrieveLimits,
        usage_port: RetrievalUsagePort,
        checkpoint: RetrievalCheckpoint,
        remaining: float,
    ):
        self.limits, self.port, self.checkpoint = limits, usage_port, checkpoint
        self._deadline = monotonic() + remaining
        self._retrieval_deadline = monotonic() + min(remaining, limits.retrieval_timeout_seconds)
        self._bounds: dict[str, tuple[int, int]] = {}

    @classmethod
    async def start(
        cls, *, handoff: AgentHandoff, limits: RetrieveLimits, usage_port: RetrievalUsagePort
    ) -> RetrievalBudget:
        limits = RetrieveLimits.model_validate(limits.model_dump())
        changes = {
            key: min(getattr(limits, key), getattr(handoff.budget, key))
            for key in (
                "max_model_attempts",
                "max_input_tokens",
                "max_output_tokens",
                "max_iterations",
                "max_tool_calls",
                "timeout_seconds",
            )
        }
        limits = limits.model_copy(update=changes)
        binding = hashlib.sha256(
            (handoff.model_dump_json() + limits.model_dump_json()).encode()
        ).hexdigest()
        loaded = await usage_port.load_checkpoint()
        checkpoint = (
            RetrievalCheckpoint.model_validate(loaded.model_dump())
            if loaded
            else RetrievalCheckpoint(binding=binding)
        )
        if checkpoint.binding != binding:
            raise RetrievalBudgetError("RETRIEVAL_CHECKPOINT_MISMATCH")
        remaining = await usage_port.remaining_seconds()
        if not math.isfinite(remaining) or remaining <= 0:
            raise RetrievalBudgetError("RETRIEVAL_DEADLINE_EXCEEDED")
        return cls(
            limits=limits,
            usage_port=usage_port,
            checkpoint=checkpoint,
            remaining=min(remaining, limits.timeout_seconds),
        )

    async def save(self, **changes: Any) -> None:
        self.checkpoint = RetrievalCheckpoint.model_validate(
            {
                **self.checkpoint.model_dump(),
                **changes,
                "version": self.checkpoint.version + 1,
            }
        )
        await self.port.save_checkpoint(self.checkpoint)

    async def remaining_seconds(self, *, retrieval: bool = False) -> float:
        remote = await self.port.remaining_seconds(retrieval=retrieval)
        if not math.isfinite(remote) or remote < 0:
            raise RetrievalBudgetError("RETRIEVAL_BUDGET_UNAVAILABLE")
        local = min(self._deadline, self._retrieval_deadline) if retrieval else self._deadline
        return max(0.0, min(remote, local - monotonic()))

    async def enter_node(self, name: str) -> None:
        if await self.remaining_seconds() <= 0:
            raise RetrievalBudgetError("RETRIEVAL_DEADLINE_EXCEEDED")
        count = self.checkpoint.nodes_used
        if count >= self.limits.max_iterations:
            raise RetrievalBudgetError("RETRIEVAL_NODE_BUDGET_EXCEEDED")
        await self.port.reserve_node(f"node:{count + 1}:{name}")
        await self.save(nodes_used=count + 1)

    async def reserve_model(self, *, input_tokens: int, output_tokens: int) -> ModelReservation:
        current = self.checkpoint
        limits = self.limits
        if (
            type(input_tokens) is not int
            or type(output_tokens) is not int
            or input_tokens <= 0
            or not 0 < output_tokens <= limits.max_output_tokens_per_attempt
            or current.model_attempts >= limits.max_model_attempts
            or current.input_reserved + input_tokens > limits.max_input_tokens
            or current.output_reserved + output_tokens > limits.max_output_tokens
        ):
            raise RetrievalBudgetError("RETRIEVAL_MODEL_BUDGET_EXCEEDED")
        if await self.remaining_seconds() <= 0:
            raise RetrievalBudgetError("RETRIEVAL_DEADLINE_EXCEEDED")
        attempt = current.model_attempts + 1
        receipt = await self.port.reserve_model_attempt(
            attempt_no=attempt, input_tokens=input_tokens, output_tokens=output_tokens
        )
        if receipt.attempt_no != attempt:
            raise RetrievalBudgetError("RETRIEVAL_RESERVATION_INVALID")
        self._bounds[receipt.reservation_id] = (input_tokens, output_tokens)
        await self.save(
            model_attempts=attempt,
            input_reserved=current.input_reserved + input_tokens,
            output_reserved=current.output_reserved + output_tokens,
            model_state="IN_FLIGHT",
        )
        return receipt

    async def record_model(
        self, receipt: ModelReservation, usage: ModelUsage | None, *, safe_code: str | None = None
    ) -> None:
        inputs, outputs = self._bounds[receipt.reservation_id]
        overrun = usage is not None and (
            usage.input_tokens > inputs or usage.output_tokens > outputs
        )
        code = "MODEL_USAGE_EXCEEDED" if overrun else safe_code
        await self.port.record_model_usage(receipt, usage, safe_code=code)
        current = self.checkpoint
        await self.save(
            observed_input_tokens=current.observed_input_tokens + usage.input_tokens
            if usage is not None and current.observed_input_tokens is not None
            else None,
            observed_output_tokens=current.observed_output_tokens + usage.output_tokens
            if usage is not None and current.observed_output_tokens is not None
            else None,
        )
        if overrun:
            raise RetrievalBudgetError("MODEL_USAGE_EXCEEDED")
