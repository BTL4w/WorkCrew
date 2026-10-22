"""Isolated bounded retrieval/synthesis; all I/O crosses required authorized ports."""

import asyncio
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import NAMESPACE_URL, uuid5

from work_management_ai.agents.orchestrator.contracts import ActorContextResolverPort
from work_management_ai.agents.retrieve.budget import (
    RetrievalBudget,
    RetrievalBudgetError,
    RetrievalUsagePort,
    SavedToolCall,
)
from work_management_ai.agents.retrieve.contracts import (
    GroundedSummary,
    RetrieveInput,
    RetrieveOutcome,
    RetrieveOutput,
    RetrieveUsage,
    SourceBinding,
    SourceCoverage,
    WebEvidence,
)
from work_management_ai.agents.retrieve.evaluators.grounding import verify_grounded_summary
from work_management_ai.agents.retrieve.evidence import (
    canonical_source_key,
    pack_web_evidence,
    source_is_valid,
    synthesis_sources,
)
from work_management_ai.agents.retrieve.prompts.system_v1 import PROMPT_VERSION, SYSTEM_V1
from work_management_ai.agents.retrieve.workflows.graph import RetrieveGraph, RetrieveState
from work_management_ai.model_gateway.contracts import (
    ModelGateway,
    ModelMessage,
    StructuredModelRequest,
)
from work_management_ai.model_gateway.errors import (
    ModelGatewayError,
    ModelInvalidOutputError,
    ModelRateLimitError,
    ModelTimeoutError,
)
from work_management_ai.runtime.activation import ActivationContext
from work_management_ai.runtime.contracts import (
    AgentHandoff,
    AgentId,
    AgentResult,
    AgentRunStatus,
    InteractionOutcome,
    JsonValue,
    ToolExecutionRequest,
    ToolExecutionResult,
    ToolExecutorPort,
)
from work_management_ai.runtime.daily_update_budget import MODEL_ATTEMPT_SCOPE
from work_management_ai.runtime.manifests import load_agent_manifest_resource
from work_management_ai.runtime.policy_guard import AgentPolicyError, PolicyGuard
from work_management_ai.tools.web.contracts import (
    WebReadPagesInput,
    WebReadPagesOutput,
    WebRevalidateInput,
    WebRevalidateOutput,
    WebSearchInput,
    WebSearchOutput,
)


def _safe(code: str | None, default: str = "RETRIEVAL_UNAVAILABLE") -> str:
    return code if code and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) else default


class RetrieveHarness:
    def __init__(
        self,
        *,
        model_gateway: ModelGateway,
        tool_executor: ToolExecutorPort,
        usage_port: RetrievalUsagePort,
        actor_resolver: ActorContextResolverPort,
        activation: ActivationContext | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        document_readiness: Literal["ABSENT", "PROCESSING", "FAILED"] | None = None,
    ):
        self.gateway = model_gateway
        self.tools = tool_executor
        self.document_readiness = document_readiness
        self.usage, self.actors, self.activation = usage_port, actor_resolver, activation
        self.is_cancelled = is_cancelled or (lambda: False)
        self.manifest = load_agent_manifest_resource(
            "work_management_ai.agents.retrieve", "agent.yaml"
        )

    async def run(self, handoff: AgentHandoff) -> AgentResult:
        runner = _RetrieveRun(self, handoff)
        try:
            async with asyncio.timeout(min(25, handoff.budget.timeout_seconds)):
                await runner.guard()
                runner.value = RetrieveInput.model_validate(handoff.typed_input)
                runner.budget = await RetrievalBudget.start(
                    handoff=handoff, limits=runner.value.limits, usage_port=self.usage
                )
                async with asyncio.timeout(await runner.budget.remaining_seconds()):
                    await runner.restore()
                    await RetrieveGraph().run(runner, runner.state)
                    return await runner.publish()
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return runner.failure("RETRIEVAL_TIMEOUT", outcome=RetrieveOutcome.SOURCE_UNAVAILABLE)
        except (AgentPolicyError, RetrievalBudgetError) as error:
            return runner.failure(_safe(str(error)))
        except (ValueError, TypeError):
            return runner.failure("RETRIEVAL_INPUT_OR_SOURCE_INVALID")
        except Exception:
            return runner.failure(
                "RETRIEVAL_UNAVAILABLE", outcome=RetrieveOutcome.SOURCE_UNAVAILABLE
            )


class _RetrieveRun:
    def __init__(self, harness: RetrieveHarness, handoff: AgentHandoff):
        self.harness, self.handoff = harness, handoff
        self.value: RetrieveInput
        self.budget: RetrievalBudget
        self.state = RetrieveState()

    async def guard(self) -> None:
        if self.harness.is_cancelled():
            raise asyncio.CancelledError
        actor = await self.harness.actors.resolve(self.handoff.actor)
        PolicyGuard().authorize_handoff(
            current_actor=actor,
            parent_agent_id=AgentId.ORCHESTRATOR,
            handoff=self.handoff,
            manifest=self.harness.manifest,
            activation=self.harness.activation,
            requested_skill_ids=("web_search_and_summarize@1",),
            requested_tool_ids=("web.search@1", "web.read_pages@1", "web.revalidate@1"),
        )
        if any(
            ref.organization_id != actor.organization_id for ref in self.handoff.context_references
        ):
            raise AgentPolicyError("CONTEXT_TENANT_MISMATCH")

    async def warn(self, *codes: str, partial: bool = False, source_failed: bool = False) -> None:
        current = self.budget.checkpoint
        await self.budget.save(
            limitations=tuple(dict.fromkeys((*current.limitations, *codes)))[:16],
            partial=current.partial or partial,
            source_failed=current.source_failed or source_failed,
        )

    async def execute_node(self, node: str, state: RetrieveState) -> None:
        if node in self.budget.checkpoint.completed_nodes:
            return
        if node in {"synthesize", "verify"} and self.value.answer_mode == "EVIDENCE_ONLY":
            return
        await self.guard()
        await self.budget.enter_node(node)
        if node == "validate":
            pass  # Typed input, manifest and current actor were checked before budget loading.
        elif node == "source_policy":
            if (
                self.value.source_mode in {"DOCUMENTS", "BOTH"}
                or self.value.document_refs
                or self.value.collection_ref
            ):
                state.outcome, state.stopped = RetrieveOutcome.NEEDS_INPUT, True
                state.question = (
                    "Chế độ nguồn này chưa khả dụng. Bạn muốn tra cứu thông tin công khai nào?"
                    if self.value.locale == "vi"
                    else "This source mode is unavailable. What public information should I search?"
                )
                if self.value.source_mode == "DOCUMENTS" and self.harness.document_readiness:
                    state.question = None
                    state.outcome = (
                        RetrieveOutcome.SOURCE_UNAVAILABLE
                        if self.harness.document_readiness == "FAILED"
                        else RetrieveOutcome.DOCUMENTS_NOT_READY
                    )
        elif node == "search":
            await self.search()
        elif node == "read":
            await self.read()
        elif node == "pack":
            await self.pack()
        elif node == "synthesize":
            await self.synthesize()
        elif node == "verify":
            self.verify()
        elif node == "finish":
            pass
        await self.budget.save(completed_nodes=(*self.budget.checkpoint.completed_nodes, node))

    async def restore(self) -> None:
        checkpoint = self.budget.checkpoint
        if not checkpoint.completed_nodes and not checkpoint.tool_calls:
            return
        await self.budget.enter_node("restore")
        for saved in checkpoint.tool_calls:
            if saved.status == "SUCCEEDED" and saved.tool_id != "web.revalidate":
                result = await self.tool(
                    saved.tool_id, saved.typed_input, saved.call_id, replay=True
                )
                if result.status != "SUCCEEDED":
                    raise AgentPolicyError("RETRIEVAL_REPLAY_REJECTED")
                self.accept(result, saved.tool_id, expected=saved.sources)
        if "pack" in checkpoint.completed_nodes:
            await self.pack()
        if checkpoint.model_state == "COMPLETED":
            self.state.summary = checkpoint.summary
            self.verify()

    async def tool(
        self,
        tool_id: Literal["web.search", "web.read_pages", "web.revalidate"],
        body: dict[str, JsonValue],
        call_id: str,
        *,
        replay: bool = False,
    ) -> ToolExecutionResult:
        await self.guard()
        current = self.budget.checkpoint
        saved = next((item for item in current.tool_calls if item.call_id == call_id), None)
        if saved is not None and (saved.tool_id != tool_id or saved.typed_input != body):
            raise AgentPolicyError("RETRIEVAL_CHECKPOINT_MISMATCH")
        if saved is not None and saved.status == "PENDING":
            raise RetrievalBudgetError("RETRIEVAL_RESULT_UNKNOWN")
        if saved is None:
            limits = self.budget.limits
            queries = int(tool_id == "web.search")
            pages = (
                len(WebReadPagesInput.model_validate(body).urls)
                if tool_id == "web.read_pages"
                else 0
            )
            requests = queries + pages
            if (
                len(current.tool_calls) >= limits.max_tool_calls
                or current.queries + queries > limits.max_queries
                or current.page_reads + pages > limits.max_page_reads
                or current.provider_requests + requests > limits.max_provider_requests
            ):
                raise RetrievalBudgetError("RETRIEVAL_TOOL_BUDGET_EXCEEDED")
            saved = SavedToolCall(call_id=call_id, tool_id=tool_id, typed_input=body)
            await self.budget.save(
                tool_calls=(*current.tool_calls, saved),
                queries=current.queries + queries,
                page_reads=current.page_reads + pages,
                provider_requests=current.provider_requests + requests,
            )
        remaining = await self.budget.remaining_seconds(
            retrieval=tool_id != "web.revalidate" and not replay
        )
        if remaining <= 0:
            raise RetrievalBudgetError("RETRIEVAL_DEADLINE_EXCEEDED")
        async with asyncio.timeout(remaining):
            result = await self.harness.tools.execute(
                ToolExecutionRequest(
                    agent_run_id=uuid5(NAMESPACE_URL, f"agent-run:{self.handoff.idempotency_key}"),
                    tool_id=tool_id,
                    tool_version="1.0.0",
                    call_id=call_id,
                    actor=self.handoff.actor,
                    typed_input=body,
                    idempotency_key=f"{self.handoff.idempotency_key}:{call_id}",
                )
            )
        await self.guard()
        if result.status == "REJECTED":
            raise AgentPolicyError(_safe(result.safe_error_code, "RETRIEVAL_ACCESS_REJECTED"))
        if result.status == "SUCCEEDED" and tool_id == "web.search":
            value = WebSearchInput.model_validate(body)
            output = WebSearchOutput.model_validate(result.typed_output)
            if len(output.evidence) > value.limit or any(
                source.method != "SEARCH_SNIPPET" for source in output.evidence
            ):
                raise AgentPolicyError("RETRIEVAL_TOOL_OUTPUT_INVALID")
        elif result.status == "SUCCEEDED" and tool_id == "web.read_pages":
            pages = WebReadPagesInput.model_validate(body)
            page_output = WebReadPagesOutput.model_validate(result.typed_output)
            allowed_urls = {canonical_source_key(url) for url in pages.urls}
            if len(page_output.evidence) > len(pages.urls) or any(
                source.method != "PAGE_CONTENT"
                or canonical_source_key(source.canonical_url) not in allowed_urls
                for source in page_output.evidence
            ):
                raise AgentPolicyError("RETRIEVAL_TOOL_OUTPUT_INVALID")
        bindings = tuple(
            SourceBinding(evidence_id=ref.resource_id, fingerprint=ref.fingerprint)
            for ref in result.evidence
            if ref.fingerprint is not None
        )
        updated = saved.model_copy(update={"status": result.status, "sources": bindings})
        await self.budget.save(
            tool_calls=tuple(
                updated if item.call_id == call_id else item
                for item in self.budget.checkpoint.tool_calls
            )
        )
        return result

    def accept(
        self,
        result: ToolExecutionResult,
        tool_id: str,
        *,
        expected: tuple[SourceBinding, ...] | None = None,
    ) -> None:
        if result.status != "SUCCEEDED":
            return
        output = (
            WebSearchOutput.model_validate(result.typed_output)
            if tool_id == "web.search"
            else WebReadPagesOutput.model_validate(result.typed_output)
        )
        sources = output.evidence
        refs = {ref.resource_id: ref for ref in result.evidence}
        if len(refs) != len(sources) or len(refs) != len(result.evidence):
            raise AgentPolicyError("RETRIEVAL_SOURCE_BINDING_INVALID")
        bindings = tuple(
            SourceBinding(evidence_id=s.evidence_id, fingerprint=s.fingerprint) for s in sources
        )
        if expected is not None and bindings != expected:
            raise AgentPolicyError("RETRIEVAL_SOURCE_BINDING_INVALID")
        for source in sources:
            ref = refs.get(source.evidence_id)
            if (
                not source_is_valid(source)
                or ref is None
                or ref.organization_id != self.handoff.actor.organization_id
                or ref.reference_id != source.evidence_id
                or ref.resource_type != "WEB_SOURCE_SNAPSHOT"
                or ref.version != 1
                or ref.fingerprint != source.fingerprint
                or ref.observed_at != source.observed_at
            ):
                raise AgentPolicyError("RETRIEVAL_SOURCE_BINDING_INVALID")
        self.state.sources.extend(sources)
        self.state.references.update(refs)

    async def search(self) -> None:
        queries = self.value.search_queries or (self.value.query,)
        for index, query in enumerate(queries[: self.budget.limits.max_queries]):
            if len(query) > 500:
                self.state.outcome, self.state.stopped = RetrieveOutcome.NEEDS_INPUT, True
                self.state.question = (
                    "Hãy cung cấp truy vấn công khai ngắn hơn."
                    if self.value.locale == "vi"
                    else "Please provide a shorter public search query."
                )
                return
            if self.budget.limits.results_per_query == 0:
                await self.warn("RETRIEVAL_RESULT_LIMIT_ZERO", source_failed=True)
                return
            value = WebSearchInput(
                query=query, filters=self.value.filters, limit=self.budget.limits.results_per_query
            )
            result = await self.tool(
                "web.search",
                cast(dict[str, JsonValue], value.model_dump(mode="json")),
                f"search:{index}",
            )
            if result.status == "FAILED":
                await self.warn(_safe(result.safe_error_code), partial=True, source_failed=True)
                continue
            self.accept(result, "web.search")
            output = WebSearchOutput.model_validate(result.typed_output)
            if output.limitations:
                await self.warn(
                    *(_safe(code, "SOURCE_LIMITATION") for code in output.limitations), partial=True
                )

    async def read(self) -> None:
        urls = list(self.value.explicit_public_urls)
        if re.search(
            r"\b(details?|quote)\b|chi tiết|nguyên văn",
            " ".join(self.value.search_queries or (self.value.query,)),
            re.I,
        ):
            urls.extend(source.canonical_url for source in self.state.sources)
        for index, url in enumerate(
            tuple(dict.fromkeys(urls))[: self.budget.limits.max_page_reads]
        ):
            value = WebReadPagesInput(urls=(url,))
            result = await self.tool(
                "web.read_pages",
                cast(dict[str, JsonValue], value.model_dump(mode="json")),
                f"page:{index}",
            )
            if result.status == "FAILED":
                await self.warn(_safe(result.safe_error_code), partial=True)
                continue
            self.accept(result, "web.read_pages")
            output = WebReadPagesOutput.model_validate(result.typed_output)
            if output.limitations:
                await self.warn(
                    *(_safe(code, "SOURCE_LIMITATION") for code in output.limitations), partial=True
                )

    async def pack(self) -> None:
        eligible: list[WebEvidence] = []
        filters = self.value.filters
        for source in self.state.sources:
            from urllib.parse import urlsplit

            host = (urlsplit(source.canonical_url).hostname or "").lower()

            def matches(domain: str, hostname: str = host) -> bool:
                domain = domain.encode("idna").decode().lower()
                return hostname == domain or hostname.endswith("." + domain)

            if (
                (filters.include_domains and not any(matches(d) for d in filters.include_domains))
                or any(matches(d) for d in filters.exclude_domains)
                or (
                    filters.published_after
                    and (
                        source.published_at is None or source.published_at < filters.published_after
                    )
                )
                or (
                    filters.published_before
                    and (
                        source.published_at is None
                        or source.published_at > filters.published_before
                    )
                )
            ):
                await self.warn("REQUIRED_FILTER_UNVERIFIED", partial=True)
                continue
            eligible.append(source)
        pages = {
            canonical_source_key(s.canonical_url): s for s in eligible if s.method == "PAGE_CONTENT"
        }
        selected: list[WebEvidence] = []
        for source in eligible:
            page = pages.get(canonical_source_key(source.canonical_url))
            if page is not None and page != source:
                selected.append(page)
            selected.append(source)
        pack = pack_web_evidence(tuple(selected), self.budget.limits)
        previous = self.budget.checkpoint.pack_fingerprint
        if previous is not None and previous != pack.fingerprint:
            raise AgentPolicyError("RETRIEVAL_SOURCE_BINDING_INVALID")
        self.state.pack = pack
        await self.warn(
            *pack.limitations, partial=bool(set(pack.limitations) - {"SOURCE_FRESHNESS_UNKNOWN"})
        )
        await self.budget.save(
            pack_fingerprint=pack.fingerprint,
            source_bindings=tuple(
                SourceBinding(evidence_id=s.evidence_id, fingerprint=s.fingerprint)
                for s in pack.sources
            ),
        )
        if not pack.sources:
            self.state.outcome = (
                RetrieveOutcome.SOURCE_UNAVAILABLE
                if self.budget.checkpoint.source_failed or self.budget.checkpoint.partial
                else RetrieveOutcome.NOT_FOUND
            )
            self.state.stopped = True

    async def revalidate(self, label: str) -> None:
        pack = self.state.pack
        if pack is None or not pack.sources:
            return
        if any(not source_is_valid(source) for source in pack.sources):
            raise AgentPolicyError("RETRIEVAL_SOURCE_EXPIRED")
        value = WebRevalidateInput(
            sources=tuple(
                SourceBinding(evidence_id=s.evidence_id, fingerprint=s.fingerprint)
                for s in pack.sources
            )
        )
        result = await self.tool(
            "web.revalidate", cast(dict[str, JsonValue], value.model_dump(mode="json")), label
        )
        output = WebRevalidateOutput.model_validate(result.typed_output)
        if result.status != "SUCCEEDED" or set(output.valid_evidence_ids) != {
            s.evidence_id for s in pack.sources
        }:
            raise AgentPolicyError("RETRIEVAL_SOURCE_UNAVAILABLE")
        if set(ref.resource_id for ref in result.evidence) != {s.evidence_id for s in pack.sources}:
            raise AgentPolicyError("RETRIEVAL_SOURCE_BINDING_INVALID")
        for ref in result.evidence:
            if ref != self.state.references.get(ref.resource_id):
                raise AgentPolicyError("RETRIEVAL_SOURCE_BINDING_INVALID")

    async def synthesize(self) -> None:
        pack = self.state.pack
        if pack is None or not pack.sources:
            return
        checkpoint = self.budget.checkpoint
        if checkpoint.model_state == "COMPLETED":
            self.state.summary = checkpoint.summary
            return
        if checkpoint.model_state in {"IN_FLIGHT", "FAILED"}:
            await self.warn(
                "MODEL_RESULT_UNKNOWN"
                if checkpoint.model_state == "IN_FLIGHT"
                else "SYNTHESIS_UNAVAILABLE"
            )
            return
        if self.budget.limits.synthesis_timeout_seconds <= 0:
            await self.warn("SYNTHESIS_TIME_BUDGET_EXHAUSTED")
            return
        await self.revalidate("pre-synthesis")
        messages = (
            ModelMessage(role="system", content=SYSTEM_V1),
            ModelMessage(
                role="user",
                content=json.dumps(
                    {
                        "public_query": " ".join(self.value.search_queries or (self.value.query,)),
                        "locale": self.value.locale,
                        "untrusted_sources": synthesis_sources(pack.sources),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        inputs = (
            sum(len(m.content.encode()) for m in messages)
            + len(json.dumps(GroundedSummary.model_json_schema()).encode())
            + 1024
        )
        outputs = min(
            1600,
            self.budget.limits.max_output_tokens_per_attempt,
            self.budget.limits.max_output_tokens - self.budget.checkpoint.output_reserved,
        )
        while self.budget.checkpoint.model_attempts < self.budget.limits.max_model_attempts:
            await self.guard()
            try:
                if self.budget.checkpoint.model_attempts:
                    await self.budget.enter_node("synthesis_retry")
                receipt = await self.budget.reserve_model(
                    input_tokens=inputs, output_tokens=outputs
                )
            except RetrievalBudgetError as error:
                await self.warn(error.safe_code)
                return
            remaining = await self.budget.remaining_seconds()
            if remaining <= 0:
                raise RetrievalBudgetError("RETRIEVAL_DEADLINE_EXCEEDED")
            token = MODEL_ATTEMPT_SCOPE.set(receipt.attempt_no)
            try:
                timeout = min(20, self.budget.limits.synthesis_timeout_seconds, remaining)
                async with asyncio.timeout(timeout):
                    response = await self.harness.gateway.generate_structured(
                        StructuredModelRequest(
                            invocation_key=PROMPT_VERSION,
                            messages=messages,
                            output_schema=GroundedSummary,
                            timeout_seconds=timeout,
                            max_output_tokens=outputs,
                        )
                    )
                await self.budget.record_model(receipt, response.usage)
                if response.usage is None:
                    await self.warn("MODEL_USAGE_UNAVAILABLE")
                summary = GroundedSummary.model_validate(response.parsed.model_dump())
                await self.budget.save(model_state="COMPLETED", summary=summary)
                self.state.summary = summary
                return
            except (ModelGatewayError, TimeoutError) as error:
                retryable = isinstance(error, (ModelTimeoutError, ModelRateLimitError))
                code = (
                    "MODEL_INVALID_OUTPUT"
                    if isinstance(error, ModelInvalidOutputError)
                    else "MODEL_UNAVAILABLE"
                )
                try:
                    await self.budget.record_model(
                        receipt,
                        error.usage if isinstance(error, ModelInvalidOutputError) else None,
                        safe_code=code,
                    )
                except RetrievalBudgetError as usage_error:
                    await self.budget.save(model_state="FAILED")
                    await self.warn(usage_error.safe_code)
                    return
                await self.budget.save(model_state="RETRYABLE" if retryable else "FAILED")
                await self.warn(code)
                if not retryable:
                    return
            except (RetrievalBudgetError, ValueError) as error:
                await self.budget.save(model_state="FAILED")
                await self.warn(
                    error.safe_code
                    if isinstance(error, RetrievalBudgetError)
                    else "MODEL_INVALID_OUTPUT"
                )
                return
            finally:
                MODEL_ATTEMPT_SCOPE.reset(token)
        await self.warn("SYNTHESIS_BUDGET_EXHAUSTED")

    def verify(self) -> None:
        if self.state.summary is None or self.state.pack is None:
            return
        verdict = verify_grounded_summary(self.state.summary, self.state.pack)
        self.state.verdict = verdict
        if not verdict.passed:
            self.state.summary = None

    def usage(self) -> RetrieveUsage:
        budget = getattr(self, "budget", None)
        checkpoint = budget.checkpoint if budget else None
        return RetrieveUsage(
            model_attempts=checkpoint.model_attempts if checkpoint else 0,
            tool_calls=len(checkpoint.tool_calls) if checkpoint else 0,
            provider_requests=checkpoint.provider_requests if checkpoint else 0,
            input_tokens=checkpoint.observed_input_tokens if checkpoint else None,
            output_tokens=checkpoint.observed_output_tokens if checkpoint else None,
        )

    def result(self, output: RetrieveOutput) -> AgentResult:
        outcome = output.outcome
        failed = outcome in {RetrieveOutcome.SOURCE_UNAVAILABLE, RetrieveOutcome.REJECTED}
        needs_input = outcome == RetrieveOutcome.NEEDS_INPUT
        usage = self.usage()
        return AgentResult(
            agent_id=AgentId.RETRIEVE,
            agent_version="1.0.0",
            status=AgentRunStatus.FAILED
            if failed
            else AgentRunStatus.AWAITING_INPUT
            if needs_input
            else AgentRunStatus.COMPLETED,
            interaction_outcome=InteractionOutcome.FAILED
            if failed
            else InteractionOutcome.NEEDS_INPUT
            if needs_input
            else InteractionOutcome.RESULT_READY,
            typed_output=cast(dict[str, JsonValue], output.model_dump(mode="json")),
            evidence=tuple(self.state.references[s.evidence_id] for s in output.evidence),
            verifier_results=(self.state.verdict,) if self.state.verdict else (),
            model_attempts_used=usage.model_attempts,
            tool_calls_used=usage.tool_calls,
            iterations_used=self.budget.checkpoint.nodes_used if hasattr(self, "budget") else 0,
            stop_reason=outcome.value,
            safe_error_code=output.safe_error_code,
        )

    async def publish(self) -> AgentResult:
        await self.guard()
        if await self.budget.remaining_seconds() <= 0:
            raise RetrievalBudgetError("RETRIEVAL_DEADLINE_EXCEEDED")
        await self.revalidate("publish")
        checkpoint = self.budget.checkpoint
        pack = self.state.pack
        sources = pack.sources if pack else ()
        outcome = self.state.outcome or (
            RetrieveOutcome.PARTIAL
            if checkpoint.partial
            else RetrieveOutcome.ANSWER_READY
            if self.state.summary
            else RetrieveOutcome.EVIDENCE_READY
        )
        warnings = (
            (*checkpoint.limitations, *self.state.summary.limitations)
            if self.state.summary
            else checkpoint.limitations
        )
        if self.state.verdict and not self.state.verdict.passed:
            warnings = (*warnings, *self.state.verdict.safe_codes)
        coverage = (
            (
                SourceCoverage(
                    source="WEB",
                    state="PARTIAL"
                    if checkpoint.partial
                    else "COMPLETE"
                    if sources
                    else "UNAVAILABLE"
                    if checkpoint.source_failed
                    else "NOT_FOUND",
                ),
            )
            if self.state.question is None
            else ()
        )
        document_only = self.value.source_mode == "DOCUMENTS" and self.harness.document_readiness
        if document_only:
            coverage = (
                SourceCoverage(
                    source="DOCUMENTS",
                    state="UNAVAILABLE"
                    if self.harness.document_readiness == "FAILED"
                    else "NOT_READY",
                ),
            )
        return self.result(
            RetrieveOutput(
                outcome=outcome,
                strategy_used="NONE" if document_only or self.state.question is not None else "WEB",
                summary=self.state.summary,
                evidence=sources,
                coverage=coverage,
                limitations=tuple(dict.fromkeys(warnings))[:16],
                question=self.state.question,
                observed_at=datetime.now(UTC),
                usage=self.usage(),
            )
        )

    def failure(
        self, code: str, *, outcome: RetrieveOutcome = RetrieveOutcome.REJECTED
    ) -> AgentResult:
        return self.result(
            RetrieveOutput(
                outcome=outcome,
                strategy_used="NONE",
                observed_at=datetime.now(UTC),
                safe_error_code=code,
                usage=self.usage(),
            )
        )
