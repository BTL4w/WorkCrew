"""Synthetic web snapshots and boundary doubles; no hosted I/O or permissive runtime defaults."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Literal, cast
from uuid import uuid4

from pydantic import BaseModel

from work_management_ai.agents.retrieve.contracts import (
    GroundedClaim,
    GroundedSummary,
    RetrieveInput,
    RetrieveUsage,
    WebEvidence,
)
from work_management_ai.model_gateway.contracts import (
    ModelUsage,
    StructuredModelRequest,
    StructuredModelResponse,
)
from work_management_ai.model_gateway.mock import MockModelGateway
from work_management_ai.runtime.activation import ActivationContext
from work_management_ai.runtime.contracts import (
    ActorReference,
    AgentBudget,
    AgentHandoff,
    AgentId,
    ContextReference,
    JsonValue,
    ResolvedActorContext,
    ToolExecutionRequest,
    ToolExecutionResult,
)
from work_management_ai.tools.web.contracts import (
    WebReadPagesOutput,
    WebRevalidateInput,
    WebRevalidateOutput,
    WebSearchOutput,
)

if TYPE_CHECKING:
    from work_management_ai.agents.retrieve.budget import ModelReservation, RetrievalCheckpoint
    from work_management_ai.agents.retrieve.harness import RetrieveHarness

PROMPT_KEY = "retrieve.synthesis.v1"


def source(
    text: str = "Review the public plan before choosing a deadline.",
    *,
    url: str = "https://example.org/guide",
    method: Literal["SEARCH_SNIPPET", "PAGE_CONTENT"] = "SEARCH_SNIPPET",
) -> WebEvidence:
    now = datetime.now(UTC)
    return WebEvidence(
        evidence_id=uuid4(),
        canonical_url=url,
        title="Public guide",
        excerpt=text,
        method=method,
        fingerprint=hashlib.sha256(text.encode()).hexdigest(),
        observed_at=now,
        expires_at=now + timedelta(days=1),
        provider_version="mock.web.v1",
    )


def handoff(locale: Literal["vi", "en"] = "en", **changes: object) -> AgentHandoff:
    value = RetrieveInput.model_validate(
        {
            "query": "public planning",
            "locale": locale,
            "requirements": [{"requirement_id": "topic", "description": "Planning guidance"}],
            **changes,
        }
    )
    return AgentHandoff(
        orchestration_run_id=uuid4(),
        parent_agent_run_id=uuid4(),
        target_agent_id=AgentId.RETRIEVE,
        target_agent_version="1.0.0",
        capability="retrieval.summarize_web",
        objective="PRIVATE OBJECTIVE MUST NOT ENTER MODEL",
        typed_input=cast(dict[str, JsonValue], value.model_dump(mode="json")),
        context_references=(),
        actor=ActorReference(organization_id=uuid4(), membership_id=uuid4()),
        budget=AgentBudget(
            max_model_attempts=2,
            max_input_tokens=12000,
            max_output_tokens=3200,
            max_iterations=12,
            max_tool_calls=8,
            timeout_seconds=25,
        ),
        step_id="retrieve-1",
        idempotency_key=str(uuid4()),
    )


class Actors:
    def __init__(self, reference: ActorReference):
        self.context = ResolvedActorContext(
            **reference.model_dump(), role="EMPLOYEE", is_active=True
        )

    async def resolve(self, reference: ActorReference) -> ResolvedActorContext:
        return self.context


class Usage:
    """Storage boundary only. Actual controller limits are exercised by the Harness."""

    def __init__(self):
        self.checkpoint: RetrievalCheckpoint | None = None
        self.started = monotonic()
        self.elapsed = 0.0
        self.models: dict[int, ModelReservation] = {}
        self.records: list[tuple[ModelReservation, ModelUsage | None, str | None]] = []
        self.nodes: list[str] = []
        self.denied = False

    async def remaining_seconds(self, *, retrieval: bool = False) -> float:
        return max(0.0, (8 if retrieval else 25) - self.elapsed - (monotonic() - self.started))

    async def load_checkpoint(self) -> RetrievalCheckpoint | None:
        return self.checkpoint.model_copy(deep=True) if self.checkpoint else None

    async def save_checkpoint(self, checkpoint: RetrievalCheckpoint) -> None:
        self.checkpoint = checkpoint.model_copy(deep=True)

    async def reserve_node(self, operation_key: str) -> None:
        self.nodes.append(operation_key)

    async def reserve_model_attempt(
        self,
        *,
        attempt_no: int,
        input_tokens: int,
        output_tokens: int,
    ) -> ModelReservation:
        from work_management_ai.agents.retrieve.budget import ModelReservation, RetrievalBudgetError

        if self.denied or attempt_no in self.models:
            raise RetrievalBudgetError("RETRIEVAL_RESERVATION_UNAVAILABLE")
        receipt = ModelReservation(reservation_id=str(uuid4()), attempt_no=attempt_no)
        self.models[attempt_no] = receipt
        return receipt

    async def record_model_usage(
        self,
        reservation: ModelReservation,
        usage: ModelUsage | None,
        *,
        safe_code: str | None = None,
    ) -> None:
        self.records.append((reservation, usage, safe_code))


class Gateway(MockModelGateway):
    def __init__(
        self,
        sources: tuple[WebEvidence, ...],
        *,
        fixture: object | None = None,
        sequence: tuple[object, ...] | None = None,
        known_usage: bool = True,
    ):
        summary = (
            GroundedSummary(
                claims=(
                    GroundedClaim(
                        text="Review the public plan.",
                        evidence_ids=(sources[0].evidence_id,),
                    ),
                )
            )
            if sources
            else GroundedSummary(insufficient_evidence=True)
        )
        super().__init__(
            fixtures={PROMPT_KEY: fixture if fixture is not None else summary},
            sequence_fixtures={PROMPT_KEY: sequence} if sequence else None,
            usage_fixtures={PROMPT_KEY: ModelUsage(100, 50)} if known_usage else None,
        )
        self.requests: list[StructuredModelRequest[BaseModel]] = []

    async def generate_structured[T: BaseModel](
        self,
        request: StructuredModelRequest[T],
    ) -> StructuredModelResponse[T]:
        self.requests.append(cast(StructuredModelRequest[BaseModel], request))
        return await super().generate_structured(request)


class Tools:
    def __init__(
        self,
        request: AgentHandoff,
        sources: tuple[WebEvidence, ...],
        pages: tuple[WebEvidence, ...] = (),
    ):
        self.actor, self.sources, self.pages = request.actor, sources, pages
        self.calls: list[ToolExecutionRequest] = []
        self.cache: dict[str, ToolExecutionResult] = {}
        self.revoked = False
        self.search_error: str | None = None

    def refs(self, sources: tuple[WebEvidence, ...]) -> tuple[ContextReference, ...]:
        return tuple(
            ContextReference(
                reference_id=s.evidence_id,
                organization_id=self.actor.organization_id,
                resource_type="WEB_SOURCE_SNAPSHOT",
                resource_id=s.evidence_id,
                version=1,
                fingerprint=s.fingerprint,
                observed_at=s.observed_at,
            )
            for s in sources
        )

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        self.calls.append(request)
        if self.revoked:
            return ToolExecutionResult(
                status="REJECTED", typed_output={}, safe_error_code="RETRIEVAL_SOURCE_UNAVAILABLE"
            )
        if request.tool_id == "web.revalidate":
            value = WebRevalidateInput.model_validate(request.typed_input)
            wanted = {item.evidence_id for item in value.sources}
            selected = tuple(s for s in (*self.sources, *self.pages) if s.evidence_id in wanted)
            output = WebRevalidateOutput(
                valid_evidence_ids=tuple(s.evidence_id for s in selected),
                observed_at=datetime.now(UTC),
            )
            return ToolExecutionResult(
                status="SUCCEEDED",
                typed_output=cast(dict[str, JsonValue], output.model_dump(mode="json")),
                evidence=self.refs(selected),
            )
        if request.call_id in self.cache:
            return self.cache[request.call_id]
        if request.tool_id == "web.search":
            if self.search_error:
                return ToolExecutionResult(
                    status="FAILED", typed_output={}, safe_error_code=self.search_error
                )
            selected = self.sources
            output = WebSearchOutput(
                evidence=selected, usage=RetrieveUsage(provider_requests=1, tool_calls=1)
            )
        elif request.tool_id == "web.read_pages":
            selected = tuple(
                s
                for s in self.pages
                if s.canonical_url in cast(list[str], request.typed_input["urls"])
            )
            output = WebReadPagesOutput(
                evidence=selected,
                limitations=() if selected else ("CACHE_UNAVAILABLE",),
                usage=RetrieveUsage(provider_requests=1, tool_calls=1),
            )
        else:
            raise AssertionError(f"forbidden tool: {request.tool_id}")
        result = ToolExecutionResult(
            status="SUCCEEDED",
            typed_output=cast(dict[str, JsonValue], output.model_dump(mode="json")),
            evidence=self.refs(selected),
        )
        self.cache[request.call_id] = result
        return result


def harness(
    request: AgentHandoff,
    tools: Tools,
    gateway: Gateway,
    usage: Usage,
    *,
    actors: Actors | None = None,
    active: bool = True,
) -> RetrieveHarness:
    from work_management_ai.agents.retrieve.harness import RetrieveHarness

    return RetrieveHarness(
        model_gateway=gateway,
        tool_executor=tools,
        usage_port=usage,
        actor_resolver=actors or Actors(request.actor),
        activation=ActivationContext(
            active_phase=5,
            core_exit_verified=True,
            active_tracks=frozenset({"I3"}),
            provider_ready=active,
        ),
    )
