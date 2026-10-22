"""Translate web tools to authorized retrieval services; never call a provider in SQL."""

from decimal import Decimal
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from app.modules.assistant.adapters.work_tools import CurrentActorResolverPort
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.retrieval.application.budget_service import RetrievalBudgetService
from app.modules.retrieval.application.service import RetrievalService
from app.modules.retrieval.domain.models import (
    PublicPageRequest,
    PublicSearchRequest,
    RetrievalPolicyError,
    RetrievalProviderError,
    RetrievalReservation,
    ValidatedSourceFilters,
)
from app.modules.retrieval.domain.public_urls import validate_public_url
from app.modules.retrieval.domain.state import RetrievalLease, RetrievalSnapshot
from work_management_ai.agents.retrieve.contracts import RetrieveOutput, RetrieveUsage, WebEvidence
from work_management_ai.runtime.contracts import (
    ActorReference,
    AgentId,
    AgentResult,
    AgentRunStatus,
    ContextReference,
    JsonValue,
    ToolExecutionRequest,
    ToolExecutionResult,
)
from work_management_ai.runtime.tool_registry import ToolRegistry, ToolRegistryError
from work_management_ai.tools.web.contracts import (
    WebReadPagesInput,
    WebReadPagesOutput,
    WebRevalidateInput,
    WebRevalidateOutput,
    WebSearchInput,
    WebSearchOutput,
)


def _web(snapshot: RetrievalSnapshot) -> WebEvidence:
    source = snapshot.source
    return WebEvidence(
        evidence_id=snapshot.id,
        canonical_url=source.url.value,
        title=source.title,
        excerpt=source.text,
        method=source.method,
        fingerprint=source.fingerprint,
        published_at=source.published_at,
        source_fetched_at=source.source_fetched_at,
        observed_at=source.observed_at,
        expires_at=snapshot.expires_at,
        provider_version=source.provider_version,
    )


def _refs(sources: tuple[RetrievalSnapshot, ...]) -> tuple[ContextReference, ...]:
    return tuple(
        ContextReference(
            reference_id=source.id,
            organization_id=source.organization_id,
            resource_type="WEB_SOURCE_SNAPSHOT",
            resource_id=source.id,
            version=1,
            fingerprint=source.source.fingerprint,
            observed_at=source.source.observed_at,
        )
        for source in sources
    )


class RetrievalToolExecutor:
    def __init__(
        self,
        *,
        budgets: RetrievalBudgetService,
        service: RetrievalService,
        actor_resolver: CurrentActorResolverPort,
        tool_registry: ToolRegistry,
        lease: RetrievalLease,
    ) -> None:
        self._budgets, self._service, self._actors, self._registry, self._lease = (
            budgets,
            service,
            actor_resolver,
            tool_registry,
            lease,
        )

    @staticmethod
    def _reject(code: str) -> ToolExecutionResult:
        return ToolExecutionResult(status="REJECTED", typed_output={}, safe_error_code=code)

    async def _actor(self, request: ToolExecutionRequest) -> AuthenticatedActor:
        actor = await self._actors.resolve(
            organization_id=request.actor.organization_id, membership_id=request.actor.membership_id
        )
        if (
            actor is None
            or actor.organization_id != request.actor.organization_id
            or actor.membership_id != request.actor.membership_id
        ):
            raise RetrievalPolicyError("RETRIEVAL_ACTOR_UNAVAILABLE")
        try:
            tool = self._registry.resolve(f"{request.tool_id}@1")
        except ToolRegistryError:
            raise RetrievalPolicyError("TOOL_NOT_ALLOWED") from None
        if (
            request.tool_id not in {"web.search", "web.read_pages", "web.revalidate"}
            or request.tool_version != tool.manifest.version
            or actor.role.value not in tool.manifest.roles
        ):
            raise RetrievalPolicyError("TOOL_NOT_ALLOWED")
        return actor

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        try:
            actor = await self._actor(request)
            await self._budgets.start_run(actor, request.agent_run_id, self._lease)
            if request.tool_id == "web.search":
                value = WebSearchInput.model_validate(request.typed_input)
            elif request.tool_id == "web.read_pages":
                value = WebReadPagesInput.model_validate(request.typed_input)
            else:
                value = WebRevalidateInput.model_validate(request.typed_input)
            tool_receipt = await self._budgets.reserve_attempt(
                actor, request.agent_run_id, f"tool:{request.call_id}", 1, "TOOL", lease=self._lease
            )
            invocation_id = uuid5(NAMESPACE_URL, f"tool:{request.agent_run_id}:{request.call_id}")
            if isinstance(value, WebRevalidateInput):
                sources = await self._budgets.load_evidence(
                    actor,
                    tuple(item.evidence_id for item in value.sources),
                    expected_hashes=tuple(item.fingerprint for item in value.sources),
                    lease=self._lease,
                    expected_run_id=request.agent_run_id,
                )
                from datetime import UTC, datetime

                output = WebRevalidateOutput(
                    valid_evidence_ids=tuple(item.id for item in sources),
                    observed_at=datetime.now(UTC),
                )
                await self._budgets.record_attempt_result(
                    actor, tool_receipt, lease=self._lease, metadata={}
                )
                return ToolExecutionResult(
                    status="SUCCEEDED",
                    typed_output=cast(dict[str, JsonValue], output.model_dump(mode="json")),
                    evidence=_refs(sources),
                )
            return await self._read(actor, request, value, tool_receipt, invocation_id)
        except RetrievalPolicyError as error:
            return self._reject(str(error))
        except ValueError:
            return self._reject("TOOL_INPUT_INVALID")

    async def _read(
        self,
        actor: AuthenticatedActor,
        request: ToolExecutionRequest,
        value: WebSearchInput | WebReadPagesInput,
        tool_receipt: RetrievalReservation,
        invocation_id: UUID,
    ) -> ToolExecutionResult:
        selected: list[RetrievalSnapshot] = []
        warnings: list[str] = []
        requests = 0
        costs: list[Decimal | None] = []
        count = 1 if isinstance(value, WebSearchInput) else len(value.urls)
        for index in range(count):
            if isinstance(value, WebSearchInput):
                filters = value.filters
                operation: PublicSearchRequest | PublicPageRequest = PublicSearchRequest(
                    value.query,
                    ValidatedSourceFilters(
                        filters.include_domains,
                        filters.exclude_domains,
                        filters.published_after,
                        filters.published_before,
                    ),
                    value.limit,
                )
            else:
                operation = PublicPageRequest(validate_public_url(value.urls[index]))
            receipt = await self._budgets.reserve_attempt(
                actor,
                request.agent_run_id,
                f"io:{request.call_id}:{index}",
                1,
                "SEARCH" if isinstance(operation, PublicSearchRequest) else "PAGE_READ",
                lease=self._lease,
                request=operation,
            )
            try:
                if isinstance(operation, PublicSearchRequest):
                    batch = await self._service.search(actor, operation, receipt)
                    sources, usage = batch.sources, batch.provider_usage
                    warnings.extend(batch.warnings)
                    if batch.safe_code:
                        warnings.append(batch.safe_code)
                else:
                    read = await self._service.read_page(actor, operation, receipt)
                    sources, usage = (read.source,) if read.source else (), read.provider_usage
                    warnings.extend(read.warnings)
                    if read.safe_code:
                        warnings.append(read.safe_code)
                requests += usage.requests
                costs.append(usage.cost_usd)
                snapshots = await self._budgets.record_attempt_result(
                    actor,
                    receipt,
                    lease=self._lease,
                    metadata={"provider_requests": usage.requests, "cost_usd": usage.cost_usd},
                    sources=sources,
                    invocation_id=invocation_id,
                )
                selected.extend(snapshots)
                if len(snapshots) < len(sources):
                    warnings.append("SOURCE_LIMIT_REACHED")
                    break
            except RetrievalProviderError as error:
                requests += error.provider_requests
                costs.append(None)
                await self._budgets.record_attempt_result(
                    actor,
                    receipt,
                    lease=self._lease,
                    metadata={
                        "safe_code": error.safe_code,
                        "retryable": error.retryable,
                        "provider_requests": error.provider_requests,
                    },
                )
                warnings.append(error.safe_code)
                break
        await self._budgets.record_attempt_result(
            actor, tool_receipt, lease=self._lease, metadata={}
        )
        evidence = tuple(_web(source) for source in selected)
        usage_output = RetrieveUsage(
            tool_calls=1,
            provider_requests=requests,
            provider_cost_usd=sum((cost for cost in costs if cost is not None), start=Decimal(0))
            if costs and all(cost is not None for cost in costs)
            else None,
        )
        limits = tuple(dict.fromkeys(warnings))[:8]
        output = (
            WebSearchOutput(evidence=evidence, limitations=limits, usage=usage_output)
            if isinstance(value, WebSearchInput)
            else WebReadPagesOutput(evidence=evidence, limitations=limits, usage=usage_output)
        )
        return ToolExecutionResult(
            status="SUCCEEDED",
            typed_output=cast(dict[str, JsonValue], output.model_dump(mode="json")),
            evidence=_refs(tuple(selected)),
        )

    async def revalidate_cached(
        self, request: ToolExecutionRequest, result: ToolExecutionResult
    ) -> ToolExecutionResult:
        try:
            actor = await self._actor(request)
            await self._budgets.validate_run(actor, request.agent_run_id, self._lease)
            sources: tuple[RetrievalSnapshot, ...] = ()
            if result.evidence:
                if any(
                    ref.resource_type != "WEB_SOURCE_SNAPSHOT"
                    or ref.organization_id != actor.organization_id
                    or ref.fingerprint is None
                    for ref in result.evidence
                ):
                    raise RetrievalPolicyError("RETRIEVAL_SOURCE_UNAVAILABLE")
                sources = await self._budgets.load_evidence(
                    actor,
                    tuple(ref.resource_id for ref in result.evidence),
                    expected_hashes=tuple(ref.fingerprint or "" for ref in result.evidence),
                    lease=self._lease,
                    expected_run_id=request.agent_run_id,
                    invocation_id=None
                    if request.tool_id == "web.revalidate"
                    else uuid5(NAMESPACE_URL, f"tool:{request.agent_run_id}:{request.call_id}"),
                )
            if result.evidence != _refs(sources):
                raise RetrievalPolicyError("RETRIEVAL_SOURCE_UNAVAILABLE")
            if result.status == "SUCCEEDED":
                if request.tool_id == "web.revalidate":
                    value = WebRevalidateInput.model_validate(request.typed_input)
                    output = WebRevalidateOutput.model_validate(result.typed_output)
                    if output.valid_evidence_ids != tuple(source.id for source in sources) or tuple(
                        (item.evidence_id, item.fingerprint) for item in value.sources
                    ) != tuple((source.id, source.source.fingerprint) for source in sources):
                        raise RetrievalPolicyError("RETRIEVAL_SOURCE_UNAVAILABLE")
                else:
                    pack = (
                        WebSearchOutput.model_validate(result.typed_output)
                        if request.tool_id == "web.search"
                        else WebReadPagesOutput.model_validate(result.typed_output)
                    )
                    if pack.evidence != tuple(_web(source) for source in sources):
                        raise RetrievalPolicyError("RETRIEVAL_SOURCE_UNAVAILABLE")
            elif result.evidence or result.typed_output:
                raise RetrievalPolicyError("RETRIEVAL_RESULT_INVALID")
            return result
        except RetrievalPolicyError as error:
            return self._reject(str(error))
        except ValueError:
            return self._reject("RETRIEVAL_RESULT_INVALID")

    async def revalidate_agent_result(
        self, run_id: UUID, reference: ActorReference, result: AgentResult
    ) -> AgentResult:
        try:
            actor = await self._actors.resolve(
                organization_id=reference.organization_id, membership_id=reference.membership_id
            )
            if (
                actor is None
                or actor.organization_id != reference.organization_id
                or actor.membership_id != reference.membership_id
            ):
                raise RetrievalPolicyError("RETRIEVAL_ACTOR_UNAVAILABLE")
            await self._budgets.validate_run(actor, run_id, self._lease, allow_terminal=True)
            if (
                result.agent_id is not AgentId.RETRIEVE
                or result.agent_version != "1.0.0"
                or result.proposed_actions
                or result.requested_handoff is not None
            ):
                raise RetrievalPolicyError("RETRIEVAL_RESULT_INVALID")
            sources: tuple[RetrievalSnapshot, ...] = ()
            if result.evidence:
                if any(
                    ref.resource_type != "WEB_SOURCE_SNAPSHOT"
                    or ref.organization_id != actor.organization_id
                    or ref.fingerprint is None
                    for ref in result.evidence
                ):
                    raise RetrievalPolicyError("RETRIEVAL_SOURCE_UNAVAILABLE")
                sources = await self._budgets.load_evidence(
                    actor,
                    tuple(ref.resource_id for ref in result.evidence),
                    expected_hashes=tuple(ref.fingerprint or "" for ref in result.evidence),
                    lease=self._lease,
                    expected_run_id=run_id,
                )
            if result.evidence != _refs(sources):
                raise RetrievalPolicyError("RETRIEVAL_SOURCE_UNAVAILABLE")
            if result.typed_output:
                output = RetrieveOutput.model_validate(result.typed_output)
                if output.evidence != tuple(_web(source) for source in sources):
                    raise RetrievalPolicyError("RETRIEVAL_SOURCE_UNAVAILABLE")
            elif result.evidence or result.status is AgentRunStatus.COMPLETED:
                raise RetrievalPolicyError("RETRIEVAL_RESULT_INVALID")
            return result
        except (RetrievalPolicyError, ValueError) as error:
            code = (
                str(error)
                if isinstance(error, RetrievalPolicyError)
                else "RETRIEVAL_RESULT_INVALID"
            )
            return AgentResult(
                agent_id=AgentId.RETRIEVE,
                agent_version="1.0.0",
                status=AgentRunStatus.FAILED,
                typed_output={},
                stop_reason="RETRIEVAL_REPLAY_REJECTED",
                safe_error_code=code,
            )
