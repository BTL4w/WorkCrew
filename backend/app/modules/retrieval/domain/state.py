"""Durable retrieval state and conservative reservation arithmetic."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.modules.retrieval.domain.models import (
    PublicPageRequest,
    PublicSearchRequest,
    RetrievalPolicyError,
    RetrievalReservation,
    RetrievedSource,
)

HARD_CAPS = {
    "queries": 2,
    "page_reads": 3,
    "provider_requests": 8,
    "tool_calls": 8,
    "nodes": 12,
    "model_attempts": 2,
    "input_tokens": 12000,
    "output_tokens": 3200,
}
PARENT_KEYS = {
    "tool_calls": ("max_tool_calls", "tool_calls"),
    "nodes": ("max_iterations", "iterations"),
    "model_attempts": ("max_model_attempts", "model_attempts"),
    "input_tokens": ("max_input_tokens", "input_tokens"),
    "output_tokens": ("max_output_tokens", "output_tokens"),
}


def request_hash(request: PublicSearchRequest | PublicPageRequest | None) -> str:
    if isinstance(request, PublicSearchRequest):
        filters = request.validated_filters
        data = {
            "query": request.query,
            "limit": request.limit,
            "include": filters.include_domains,
            "exclude": filters.exclude_domains,
            "after": filters.published_after.isoformat() if filters.published_after else None,
            "before": filters.published_before.isoformat() if filters.published_before else None,
        }
    elif isinstance(request, PublicPageRequest):
        data = {"url": request.url.value}
    else:
        data = {}
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def charge_for(kind: str, bounds: tuple[int, int]) -> dict[str, int]:
    charge = dict.fromkeys(HARD_CAPS, 0)
    if kind == "SEARCH":
        charge.update(queries=1, provider_requests=1)
    elif kind == "PAGE_READ":
        charge.update(page_reads=1, provider_requests=1)
    elif kind == "TOOL":
        charge["tool_calls"] = 1
    elif kind == "NODE":
        charge["nodes"] = 1
    elif kind == "MODEL":
        inputs, outputs = bounds
        if (
            type(inputs) is not int
            or type(outputs) is not int
            or not 1 <= inputs <= 12000
            or not 1 <= outputs <= 1600
        ):
            raise RetrievalPolicyError("RETRIEVAL_TOKEN_BUDGET_EXCEEDED")
        charge.update(model_attempts=1, input_tokens=inputs, output_tokens=outputs)
    else:
        raise RetrievalPolicyError("RETRIEVAL_OPERATION_INVALID")
    return charge


@dataclass(frozen=True, slots=True)
class RetrievalLease:
    job_id: UUID
    worker_id: str
    attempt_no: int


@dataclass(frozen=True, slots=True)
class RetrievalRunScope:
    agent_run_id: UUID
    orchestration_run_id: UUID
    handoff_budget: dict[str, Any]
    parent_budget: dict[str, Any]
    parent_usage: dict[str, Any]
    typed_input: dict[str, Any]
    user_text: str
    started_at: datetime
    parent_started_at: datetime
    now: datetime
    lease_until: datetime | None
    protected_terms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetrievalBudgetState:
    organization_id: UUID
    agent_run_id: UUID
    owner_membership_id: UUID
    orchestration_run_id: UUID
    lease: RetrievalLease
    fencing_token: UUID
    scope: dict[str, Any]
    caps: dict[str, int]
    reserved: dict[str, int]
    started_at: datetime
    deadline: datetime
    expires_at: datetime
    version: int = 1


@dataclass(frozen=True, slots=True)
class RetrievalAttempt:
    receipt: RetrievalReservation
    request_hash: str
    charged: dict[str, int]
    status: str = "RESERVED"
    dispatched: bool = False
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True, slots=True)
class RetrievalSnapshot:
    id: UUID
    organization_id: UUID
    owner_membership_id: UUID
    agent_run_id: UUID
    tool_invocation_id: UUID
    source: RetrievedSource
    expires_at: datetime
