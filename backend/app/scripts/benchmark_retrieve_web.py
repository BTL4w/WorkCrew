"""Opt-in public-data benchmark. No account, billing, tenant, or activation changes."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Literal, Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from app.modules.retrieval.domain.models import (
    PageReadResult,
    PublicPageRequest,
    PublicSearchRequest,
    RetrievedSource,
    SearchBatch,
)
from app.modules.retrieval.domain.public_urls import validate_public_query, validate_public_url
from work_management_ai.agents.retrieve.contracts import (
    RetrieveInput,
    RetrieveOutput,
    RetrieveRequirement,
    WebEvidence,
)
from work_management_ai.agents.retrieve.harness import RetrieveHarness
from work_management_ai.evaluation.retrieve_web import EvaluationUsage
from work_management_ai.model_gateway.contracts import (
    ModelGateway,
    ModelUsage,
    StructuredModelRequest,
    StructuredModelResponse,
)
from work_management_ai.model_gateway.errors import ModelUnavailableError
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

_ROOT = Path(__file__).resolve().parents[3]
_CASES = _ROOT / "ai/tests/fixtures/retrieve_web/routing_cases.json"
# Conservative published-price ceilings for this exact <=5-result highlights payload.
# https://exa.ai/pricing and Search/Contents docs, checked2026-10-23.
PRICE_VERSION = "exa-auto7-highlights5-contents1.2026-10-23"
SEARCH_BOUND = Decimal("0.012")  # $7/1k Auto + up to5 highlights at $1/1k pages.
PAGE_BOUND = Decimal("0.001")  # Cache-only, text only, one URL; no summaries.
_MODEL_PRICES = {  # USD /1M tokens; default standard tier, no cache discount assumed.
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "gpt-4o-mini-2024-07-18": (Decimal("0.15"), Decimal("0.60")),
    "gpt-5.4": (Decimal("2.50"), Decimal("15")),
    "gpt-5.4-mini": (Decimal("0.75"), Decimal("4.50")),
    "gpt-5.4-mini-2026-03-17": (Decimal("0.75"), Decimal("4.50")),
}


class BudgetStop(ValueError):
    pass


@dataclass(frozen=True)
class Receipt:
    id: int
    kind: Literal["search", "page", "model"]
    bound: Decimal


class BenchmarkBudget:
    def __init__(
        self,
        *,
        max_requests: int = 80,
        retrieval_cap: Decimal = Decimal("1"),
        model_cap: Decimal = Decimal("1"),
        total_cap: Decimal = Decimal("2"),
        journal: Callable[[dict[str, object]], None] | None = None,
    ):
        if (
            not 0 <= max_requests <= 80
            or any(
                not value.is_finite() or value < 0
                for value in (retrieval_cap, model_cap, total_cap)
            )
            or retrieval_cap > 1
            or model_cap > 1
            or total_cap > 2
        ):
            raise ValueError("INVALID_BENCHMARK_CAPS")
        self.max_requests, self.retrieval_cap, self.model_cap, self.total_cap = (
            max_requests,
            retrieval_cap,
            model_cap,
            total_cap,
        )
        self.charged_retrieval = Decimal(0)
        self.charged_model = Decimal(0)
        self.request_count = 0
        self.model_attempts = 0
        self.stopped_reason: str | None = None
        self.pending: dict[int, Receipt] = {}
        self.observations: list[tuple[Receipt, Decimal | None]] = []
        self.journal = journal

    def _journal(self, event: dict[str, object]) -> None:
        try:
            assert self.journal is not None
            self.journal(event)
        except Exception:
            self.stopped_reason = "JOURNAL_UNAVAILABLE"
            raise BudgetStop("JOURNAL_UNAVAILABLE") from None

    def reserve(self, kind: Literal["search", "page", "model"], bound: Decimal) -> Receipt:
        if not bound.is_finite() or bound <= 0 or kind not in {"search", "page", "model"}:
            raise ValueError("INVALID_COST_BOUND")
        reason = self.stopped_reason or (
            "REQUEST_CAP"
            if self.request_count >= self.max_requests
            else "MODEL_CAP"
            if kind == "model" and self.charged_model + bound > self.model_cap
            else "RETRIEVAL_CAP"
            if kind != "model" and self.charged_retrieval + bound > self.retrieval_cap
            else "TOTAL_CAP"
            if self.charged_retrieval + self.charged_model + bound > self.total_cap
            else None
        )
        if reason:
            self.stopped_reason = reason
            raise BudgetStop(reason)
        self.request_count += 1
        receipt = Receipt(self.request_count, kind, bound)
        self.pending[receipt.id] = receipt
        if kind == "model":
            self.charged_model += bound
            self.model_attempts += 1
        else:
            self.charged_retrieval += bound
        if self.journal:
            self._journal(
                {"status": "RESERVED", "id": receipt.id, "kind": kind, "upper_usd": str(bound)}
            )
        return receipt

    def finish(self, receipt: Receipt, observed: Decimal | None) -> None:
        if self.pending.get(receipt.id) != receipt:
            raise ValueError("COST_RECEIPT_ALREADY_FINISHED")
        if observed is not None and (not observed.is_finite() or observed < 0):
            raise ValueError("INVALID_OBSERVED_COST")
        del self.pending[receipt.id]
        self.observations.append((receipt, observed))
        if observed is not None:
            if receipt.kind == "model":
                self.charged_model += observed - receipt.bound
            else:
                self.charged_retrieval += observed - receipt.bound
            if observed > receipt.bound:
                self.stopped_reason = "COST_BOUND_EXCEEDED"
        if self.journal:
            self._journal(
                {
                    "status": "UNKNOWN" if observed is None else "OBSERVED",
                    "id": receipt.id,
                    "kind": receipt.kind,
                    "observed_usd": str(observed) if observed is not None else None,
                }
            )


class PublicQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    case_id: str = Field(pattern=r"^[a-z0-9_-]{1,100}$")
    locale: Literal["vi", "en"]
    query: str = Field(min_length=1, max_length=500)
    public_scope: Literal[True]


def default_queries(path: Path | None = None) -> tuple[PublicQuery, ...]:
    data = json.loads((path or _CASES).read_text())
    if data.get("dataset_kind") != "PUBLIC_BENCHMARK":
        raise ValueError("PUBLIC_DATASET_REQUIRED")
    queries = tuple(PublicQuery.model_validate(row) for row in data["benchmark_queries"])
    if (
        not 1 <= len(queries) <= 80
        or len({query.query.casefold().strip() for query in queries}) != len(queries)
        or len({query.case_id for query in queries}) != len(queries)
    ):
        raise ValueError("UNIQUE_PUBLIC_QUERIES_REQUIRED")
    for query in queries:
        validate_public_query(query.query)
    return queries


class Row(BaseModel):
    case_id: str
    locale: Literal["vi", "en"]
    public_query: str
    outcome: str
    completed: bool
    duration_ms: int
    source_hashes: tuple[str, ...] = ()
    source_urls: tuple[str, ...] = ()
    published_at: tuple[datetime | None, ...] = ()
    source_fetched_at: tuple[datetime | None, ...] = ()
    observed_at: datetime
    citation_bindings: bool | None = None
    semantic_support: None = None


class BenchmarkReport(BaseModel):
    mode: Literal["MOCK", "HOSTED", "NOT_RUN"]
    pipeline: Literal["search-only", "end-to-end"]
    eligible: int
    attempted: int
    completed: int
    failed: int
    external_attempts: int = 0
    model_attempts: int = 0
    rows: tuple[Row, ...] = ()
    stopped_reason: str | None = None
    observed_retrieval_usd: Decimal | None = None
    observed_model_usd: Decimal | None = None
    conservative_retrieval_usd: Decimal = Decimal(0)
    conservative_model_usd: Decimal = Decimal(0)
    price_version: str = PRICE_VERSION
    price_provenance: str = "https://exa.ai/pricing"
    model_price_provenance: str = "https://developers.openai.com/api/docs/pricing"
    model: str | None = None
    usage_provenance: str = (
        "provider_report_or_unknown_charged_at_upper_bound;not_invoice_or_free_credit_entitlement"
    )
    latency_ms: dict[str, dict[str, float] | None] = Field(default_factory=dict)
    held_out_routing_accuracy: None = None
    top5_relevance: None = None
    answerable_evidence_coverage: None = None
    semantic_support: None = None
    freshness_accuracy: None = None
    measurement_status: Literal["NOT_RUN", "INSUFFICIENT_MEASUREMENT"] = "INSUFFICIENT_MEASUREMENT"
    hosted_ready: Literal[False] = False


class Adapter(Protocol):
    async def search(
        self, request: PublicSearchRequest, *, timeout_seconds: float
    ) -> SearchBatch: ...
    async def read(
        self, request: PublicPageRequest, *, timeout_seconds: float
    ) -> PageReadResult: ...


class _Session:
    def __init__(self, adapter: Adapter, budget: BenchmarkBudget):
        self.adapter, self.budget = adapter, budget
        self.timings: dict[str, list[float]] = {
            key: [] for key in ("search", "page_read", "synthesis", "total")
        }

    async def search(self, query: str) -> SearchBatch:
        receipt = self.budget.reserve("search", SEARCH_BOUND)
        started = monotonic()
        try:
            result = await self.adapter.search(
                PublicSearchRequest(validate_public_query(query), limit=5), timeout_seconds=8
            )
        except BaseException:
            self.budget.finish(receipt, None)
            raise
        else:
            self.budget.finish(receipt, result.provider_usage.cost_usd)
            return result
        finally:
            self.timings["search"].append((monotonic() - started) * 1000)

    async def page(self, url: str) -> PageReadResult:
        receipt = self.budget.reserve("page", PAGE_BOUND)
        started = monotonic()
        try:
            result = await self.adapter.read(
                PublicPageRequest(validate_public_url(url)), timeout_seconds=8
            )
        except BaseException:
            self.budget.finish(receipt, None)
            raise
        else:
            self.budget.finish(receipt, result.provider_usage.cost_usd)
            return result
        finally:
            self.timings["page_read"].append((monotonic() - started) * 1000)


class _Model:
    def __init__(self, gateway: ModelGateway, session: _Session, model: str):
        if model not in _MODEL_PRICES:
            raise BudgetStop("MODEL_PRICE_NOT_VERIFIED")
        self.gateway, self.session, self.model = gateway, session, model

    async def generate_structured[T: BaseModel](
        self, request: StructuredModelRequest[T]
    ) -> StructuredModelResponse[T]:
        input_price, output_price = _MODEL_PRICES[self.model]
        token_ceiling = (
            len(json.dumps(request.output_schema.model_json_schema()).encode())
            + sum(len(message.content.encode()) for message in request.messages)
            + 512
        )
        if token_ceiling > 12000 or (request.max_output_tokens or 1600) > 1600:
            raise ModelUnavailableError("BENCHMARK_INPUT_BOUND")
        try:
            receipt = self.session.budget.reserve(
                "model", (12000 * input_price + 1600 * output_price) / Decimal(1_000_000)
            )
        except BudgetStop as error:
            raise ModelUnavailableError(str(error)) from None
        started = monotonic()
        try:
            response = await self.gateway.generate_structured(request)
        except BaseException:
            self.session.budget.finish(receipt, None)
            raise
        else:
            usage = response.usage
            cost = (
                (usage.input_tokens * input_price + usage.output_tokens * output_price)
                / Decimal(1_000_000)
                if usage
                else None
            )
            self.session.budget.finish(receipt, cost)
            return response
        finally:
            self.session.timings["synthesis"].append((monotonic() - started) * 1000)


class MockBenchmarkGateway:
    async def generate_structured[T: BaseModel](
        self, request: StructuredModelRequest[T]
    ) -> StructuredModelResponse[T]:
        payload = json.loads(request.messages[-1].content)
        sources = payload.get("untrusted_sources", [])
        parsed = request.output_schema.model_validate(
            dict(
                claims=[
                    dict(
                        text="Review the public goal and constraints.",
                        evidence_ids=[sources[0]["evidence_id"]],
                    )
                ]
                if sources
                else [],
                insufficient_evidence=not sources,
            )
        )
        return StructuredModelResponse(
            parsed=parsed, model_ref="mock:benchmark", usage=ModelUsage(100, 50)
        )


class _Actor:
    def __init__(self, actor: ActorReference):
        self.actor = ResolvedActorContext(**actor.model_dump(), role="EMPLOYEE", is_active=True)

    async def resolve(self, reference: ActorReference) -> ResolvedActorContext:
        return self.actor.model_copy(
            update={
                "is_active": reference.organization_id == self.actor.organization_id
                and reference.membership_id == self.actor.membership_id
            }
        )


class _Tools:
    """Public benchmark ports only: no Work tools, database, origin HTTP or tenant data."""

    def __init__(self, session: _Session, actor: ActorReference, run: UUID):
        self.session, self.actor, self.run = session, actor, run
        self.sources: dict[UUID, WebEvidence] = {}

    def _source(self, value: RetrievedSource) -> WebEvidence:
        source = WebEvidence(
            evidence_id=uuid5(self.run, value.url.value + value.fingerprint),
            canonical_url=value.url.value,
            title=value.title,
            excerpt=value.text,
            method=value.method,
            fingerprint=value.fingerprint,
            published_at=value.published_at,
            source_fetched_at=value.source_fetched_at,
            observed_at=value.observed_at,
            expires_at=value.observed_at + timedelta(days=1),
            provider_version=value.provider_version,
        )
        self.sources[source.evidence_id] = source
        return source

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        if request.actor != self.actor:
            return ToolExecutionResult(
                status="REJECTED", typed_output={}, safe_error_code="BENCHMARK_SCOPE_MISMATCH"
            )
        sources: tuple[WebEvidence, ...] = ()
        try:
            if request.tool_id == "web.search":
                batch = await self.session.search(str(request.typed_input["query"]))
                sources = tuple(self._source(source) for source in batch.sources)
                data = WebSearchOutput(evidence=sources, limitations=batch.warnings)
            elif request.tool_id == "web.read_pages":
                urls = cast(list[str], request.typed_input["urls"])
                # Reads must be explicit or already returned by the source service.
                if any(
                    url not in {source.canonical_url for source in self.sources.values()}
                    for url in urls
                ):
                    raise ValueError("BENCHMARK_UNKNOWN_SOURCE")
                values = [await self.session.page(url) for url in urls]
                sources = tuple(
                    self._source(value.source) for value in values if value.source is not None
                )
                data = WebReadPagesOutput(
                    evidence=sources,
                    limitations=tuple(value.safe_code for value in values if value.safe_code),
                )
            elif request.tool_id == "web.revalidate":
                refs = WebRevalidateInput.model_validate(request.typed_input).sources
                sources = tuple(
                    self.sources[ref.evidence_id]
                    for ref in refs
                    if ref.evidence_id in self.sources
                    and self.sources[ref.evidence_id].fingerprint == ref.fingerprint
                )
                data = WebRevalidateOutput(
                    valid_evidence_ids=tuple(source.evidence_id for source in sources),
                    observed_at=datetime.now(UTC),
                )
            else:
                raise ValueError("BENCHMARK_FORBIDDEN_TOOL")
        except Exception:
            return ToolExecutionResult(
                status="FAILED", typed_output={}, safe_error_code="RETRIEVAL_UNAVAILABLE"
            )
        refs = tuple(
            ContextReference(
                reference_id=source.evidence_id,
                resource_id=source.evidence_id,
                organization_id=self.actor.organization_id,
                resource_type="WEB_SOURCE_SNAPSHOT",
                version=1,
                fingerprint=source.fingerprint,
                observed_at=source.observed_at,
            )
            for source in sources
        )
        return ToolExecutionResult(
            status="SUCCEEDED",
            typed_output=cast(dict[str, JsonValue], data.model_dump(mode="json")),
            evidence=refs,
        )


def _quantiles(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "p50": ordered[max(0, math.ceil(len(values) * 0.5) - 1)],
        "p95": ordered[max(0, math.ceil(len(values) * 0.95) - 1)],
        "samples": float(len(values)),
    }


async def run_benchmark(
    queries: tuple[PublicQuery, ...],
    *,
    mode: Literal["search-only", "end-to-end"],
    adapter: Adapter,
    budget: BenchmarkBudget,
    hosted: bool,
    gateway: ModelGateway | None = None,
    model: str | None = None,
) -> BenchmarkReport:
    session, rows = _Session(adapter, budget), list[Row]()
    if mode == "end-to-end" and (gateway is None or model not in _MODEL_PRICES):
        return BenchmarkReport(
            mode="NOT_RUN",
            pipeline=mode,
            eligible=len(queries),
            attempted=0,
            completed=0,
            failed=0,
            stopped_reason="MODEL_PRICE_OR_GATEWAY_UNAVAILABLE",
            measurement_status="NOT_RUN",
        )
    for query in queries:
        if budget.stopped_reason:
            break
        started = monotonic()
        citations: bool | None = None
        sources: tuple[WebEvidence, ...] = ()
        try:
            if mode == "search-only":
                batch = await session.search(query.query)
                result = tuple(batch.sources)
                sources = tuple(
                    WebEvidence(
                        evidence_id=uuid5(NAMESPACE_URL, source.url.value + source.fingerprint),
                        canonical_url=source.url.value,
                        title=source.title,
                        excerpt=source.text,
                        method=source.method,
                        fingerprint=source.fingerprint,
                        published_at=source.published_at,
                        source_fetched_at=source.source_fetched_at,
                        observed_at=source.observed_at,
                        expires_at=source.observed_at + timedelta(days=1),
                        provider_version=source.provider_version,
                    )
                    for source in result
                )
                # A valid empty result is distinct from rejected/unverifiable provider content.
                limited = (
                    not batch.filters_verified
                    or bool(set(batch.warnings) - {"NOT_FOUND"})
                    or batch.safe_code not in {None, "NOT_FOUND"}
                    or bool(sources and batch.safe_code == "NOT_FOUND")
                )
                outcome = (
                    ("PARTIAL" if limited else "EVIDENCE_READY")
                    if sources
                    else ("SOURCE_UNAVAILABLE" if limited else "NOT_FOUND")
                )
            else:
                identity = uuid5(
                    NAMESPACE_URL,
                    f"public-benchmark:{query.case_id}:{datetime.now(UTC).isoformat()}",
                )
                actor = ActorReference(
                    organization_id=uuid5(identity, "synthetic-scope"),
                    membership_id=uuid5(identity, "synthetic-actor"),
                )
                tools = _Tools(session, actor, identity)
                value = RetrieveInput(
                    query=query.query,
                    locale=query.locale,
                    requirements=(
                        RetrieveRequirement(
                            requirement_id="public", description="Public source guidance"
                        ),
                    ),
                )
                handoff = AgentHandoff(
                    orchestration_run_id=identity,
                    parent_agent_run_id=uuid5(identity, "orchestrator"),
                    target_agent_id=AgentId.RETRIEVE,
                    target_agent_version="1.0.0",
                    capability="retrieval.summarize_web",
                    objective="Evaluate explicit public source question",
                    typed_input=cast(dict[str, JsonValue], value.model_dump(mode="json")),
                    actor=actor,
                    context_references=(),
                    budget=AgentBudget(
                        max_iterations=12,
                        max_tool_calls=8,
                        max_model_attempts=2,
                        max_input_tokens=12000,
                        max_output_tokens=3200,
                        timeout_seconds=25,
                    ),
                    step_id="benchmark",
                    idempotency_key=str(identity),
                )
                output = await RetrieveHarness(
                    model_gateway=_Model(cast(ModelGateway, gateway), session, cast(str, model)),
                    tool_executor=tools,
                    usage_port=EvaluationUsage(),
                    actor_resolver=_Actor(actor),
                    activation=ActivationContext(5, True, frozenset({"I3"}), True),
                ).run(handoff)
                value_out = RetrieveOutput.model_validate(output.typed_output)
                sources = value_out.evidence
                outcome = value_out.outcome.value
                claims = value_out.summary.claims if value_out.summary else ()
                citations = all(
                    set(claim.evidence_ids) <= {source.evidence_id for source in sources}
                    for claim in claims
                )
                citations = citations and all(
                    hashlib.sha256(source.excerpt.encode()).hexdigest() == source.fingerprint
                    for source in sources
                )
        except BudgetStop:
            break
        except Exception:
            outcome = "SOURCE_UNAVAILABLE"
        duration = int((monotonic() - started) * 1000)
        session.timings["total"].append(float(duration))
        rows.append(
            Row(
                case_id=query.case_id,
                locale=query.locale,
                public_query=query.query,
                outcome=outcome,
                completed=outcome in {"ANSWER_READY", "EVIDENCE_READY", "PARTIAL", "NOT_FOUND"},
                duration_ms=duration,
                source_hashes=tuple(source.fingerprint for source in sources),
                source_urls=tuple(source.canonical_url for source in sources),
                published_at=tuple(source.published_at for source in sources),
                source_fetched_at=tuple(source.source_fetched_at for source in sources),
                observed_at=datetime.now(UTC),
                citation_bindings=citations,
            )
        )

    def observed(model_cost: bool) -> Decimal | None:
        values = [
            value
            for receipt, value in budget.observations
            if (receipt.kind == "model") == model_cost
        ]
        return (
            sum((value for value in values if value is not None), Decimal(0))
            if values and all(value is not None for value in values)
            else None
        )

    return BenchmarkReport(
        mode="HOSTED" if hosted else "MOCK",
        pipeline=mode,
        eligible=len(queries),
        attempted=len(rows),
        completed=sum(row.completed for row in rows),
        failed=sum(not row.completed for row in rows),
        rows=tuple(rows),
        external_attempts=budget.request_count,
        model_attempts=budget.model_attempts,
        stopped_reason=budget.stopped_reason,
        observed_retrieval_usd=observed(False),
        observed_model_usd=observed(True),
        conservative_retrieval_usd=budget.charged_retrieval,
        conservative_model_usd=budget.charged_model,
        model=model,
        latency_ms={
            **{key: _quantiles(values) for key, values in session.timings.items()},
            "queue": None,
        },
    )


def hosted_dependencies(
    mode: str = "search-only",
) -> tuple[Adapter, ModelGateway | None, str | None]:
    # Read credentials only behind main's explicit opt-in. Never log Settings or provider errors.
    from app.core.config import Settings
    from app.modules.planning_runs.adapters.ai_runtime import build_model_gateway
    from app.modules.retrieval.adapters.exa import ExaAdapter

    settings = Settings()
    if settings.exa_api_key is None:
        raise ValueError("EXA_KEY_REQUIRED")
    return (
        ExaAdapter(settings.exa_api_key),
        build_model_gateway(settings)
        if mode == "end-to-end" and settings.ai_provider == "openai"
        else None,
        settings.ai_model or None,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("search-only", "end-to-end"), default="search-only")
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--max-requests", type=int, default=80)
    parser.add_argument("--max-retrieval-usd", type=Decimal, default=Decimal("1"))
    parser.add_argument("--max-model-usd", type=Decimal, default=Decimal("1"))
    parser.add_argument("--max-total-usd", type=Decimal, default=Decimal("2"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--confirm-hosted", action="store_true")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args(argv)
    exit_code = 0
    try:
        queries = default_queries(args.cases)
        output = (
            args.output
            or _ROOT
            / "docs/benchmarks"
            / f"retrieve-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"
        )
        # Check the immutable artifact destination before credentials or any provider setup.
        if output.exists() or output.with_suffix(".ledger.jsonl").exists():
            raise ValueError("BENCHMARK_ARTIFACT_EXISTS")
        budget = BenchmarkBudget(
            max_requests=args.max_requests,
            retrieval_cap=args.max_retrieval_usd,
            model_cap=args.max_model_usd,
            total_cap=args.max_total_usd,
        )
        if args.mock and args.confirm_hosted:
            raise ValueError("CONFLICTING_BENCHMARK_MODES")
        if not args.mock and not args.confirm_hosted:
            report = BenchmarkReport(
                mode="NOT_RUN",
                pipeline=args.mode,
                eligible=len(queries),
                attempted=0,
                completed=0,
                failed=0,
                stopped_reason="HOSTED_OPT_IN_REQUIRED",
                measurement_status="NOT_RUN",
            )
        else:
            if args.mock:
                from app.modules.retrieval.adapters.mock import MockRetrievalAdapter

                adapter, gateway, model = (
                    MockRetrievalAdapter(),
                    MockBenchmarkGateway() if args.mode == "end-to-end" else None,
                    "gpt-4o-mini" if args.mode == "end-to-end" else None,
                )
            else:
                adapter, gateway, model = hosted_dependencies(args.mode)
            output.parent.mkdir(parents=True, exist_ok=True)
            # Write-ahead receipts survive interruption; an unfinished reservation
            # remains conservatively charged. No query, body, key or exception is logged.
            with output.with_suffix(".ledger.jsonl").open("x") as ledger:

                def journal(event: dict[str, object]) -> None:
                    import os

                    ledger.write(json.dumps(event) + "\n")
                    ledger.flush()
                    os.fsync(ledger.fileno())

                budget.journal = journal
                report = asyncio.run(
                    run_benchmark(
                        queries,
                        mode=args.mode,
                        adapter=adapter,
                        budget=budget,
                        hosted=not args.mock,
                        gateway=gateway,
                        model=model,
                    )
                )
            if report.mode == "NOT_RUN":
                exit_code = 2
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x") as stream:
            stream.write(report.model_dump_json(indent=2) + "\n")
        print(
            json.dumps(
                {
                    "mode": report.mode,
                    "attempted": report.attempted,
                    "completed": report.completed,
                    "failed": report.failed,
                    "hosted_ready": False,
                    "reason": report.stopped_reason,
                }
            )
        )
        return exit_code
    except Exception:
        print(
            json.dumps(
                {"mode": "NOT_RUN", "safe_code": "BENCHMARK_CONFIGURATION_OR_OUTPUT_REJECTED"}
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
