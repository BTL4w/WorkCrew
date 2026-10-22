"""Own short transactions; reserve before I/O and never refund unknown attempts."""

import hashlib
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, cast
from uuid import UUID, uuid4

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.retrieval.application.ports import (
    RetrievalActivationPort,
    RetrievalRepository,
    RetrievalTransactions,
)
from app.modules.retrieval.domain.models import (
    PublicPageRequest,
    PublicSearchRequest,
    RetrievalPolicyError,
    RetrievalReservation,
    RetrievedSource,
    ValidatedSourceFilters,
)
from app.modules.retrieval.domain.public_urls import validate_public_query, validate_public_url
from app.modules.retrieval.domain.state import (
    HARD_CAPS,
    PARENT_KEYS,
    RetrievalAttempt,
    RetrievalBudgetState,
    RetrievalLease,
    RetrievalRunScope,
    RetrievalSnapshot,
    charge_for,
    request_hash,
)


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _public_scope(scope: RetrievalRunScope) -> dict[str, Any]:
    if scope.typed_input.get("source_mode") == "DOCUMENTS":
        raise RetrievalPolicyError("DOCUMENTS_NOT_READY")
    text = _normalized(scope.user_text)
    if not re.search(r"\b(web|public|internet|search)\b|công khai|tra cứu", text) or re.search(
        r"\b(confidential|internal|secret)\b|nội bộ|bí mật", text
    ):
        raise RetrievalPolicyError("PUBLIC_QUERY_PROVENANCE_REQUIRED")
    raw_values: object = scope.typed_input.get("search_queries") or [scope.typed_input.get("query")]
    if not isinstance(raw_values, list):
        raise RetrievalPolicyError("PUBLIC_QUERY_PROVENANCE_REQUIRED")
    values = cast(list[object], raw_values)
    if not 1 <= len(values) <= 2:
        raise RetrievalPolicyError("PUBLIC_QUERY_PROVENANCE_REQUIRED")
    queries: list[str] = []
    for query in values:
        if not isinstance(query, str):
            raise RetrievalPolicyError("PUBLIC_QUERY_PROVENANCE_REQUIRED")
        try:
            query = validate_public_query(query)
        except ValueError:
            raise RetrievalPolicyError("PUBLIC_QUERY_PROVENANCE_REQUIRED") from None
        if _normalized(query) not in text:
            raise RetrievalPolicyError("PUBLIC_QUERY_PROVENANCE_REQUIRED")
        queries.append(query)
    from app.modules.retrieval.application.query_policy import (
        validate_public_query as validate_intent,
    )

    spans: list[dict[str, Any]] = []
    for query in queries:
        start = scope.user_text.find(query)
        spans.append(dict(start=start, end=start + len(query), text=query))
    validate_intent(
        dict(
            search_queries=queries,
            message_spans=spans,
            filters=scope.typed_input.get("filters", {}),
        ),
        scope.user_text,
        scope.protected_terms,
    )
    raw_filters: object = scope.typed_input.get("filters", {})
    if not isinstance(raw_filters, dict):
        raise RetrievalPolicyError("PUBLIC_QUERY_PROVENANCE_REQUIRED")
    filters = cast(dict[str, object], raw_filters)
    for key in ("include_domains", "exclude_domains"):
        domains = filters.get(key, [])
        if not isinstance(domains, list):
            raise RetrievalPolicyError("PUBLIC_QUERY_PROVENANCE_REQUIRED")
        for domain in cast(list[object], domains):
            if not isinstance(domain, str) or _normalized(domain) not in text:
                raise RetrievalPolicyError("PUBLIC_QUERY_PROVENANCE_REQUIRED")
    for key in ("published_after", "published_before"):
        if filters.get(key) and str(filters[key])[:10] not in text:
            raise RetrievalPolicyError("PUBLIC_QUERY_PROVENANCE_REQUIRED")
    urls: list[str] = []
    for raw in re.findall(r"https?://\S+", scope.user_text):
        try:
            urls.append(validate_public_url(raw).value)
        except ValueError:
            continue
    if len(urls) > 3:
        raise RetrievalPolicyError("PUBLIC_QUERY_PROVENANCE_REQUIRED")
    return {
        "search_scope_hashes": [
            request_hash(PublicSearchRequest(_normalized(query), _filters(filters), 1))
            for query in queries
        ],
        "explicit_url_hashes": [
            request_hash(PublicPageRequest(validate_public_url(url))) for url in urls
        ],
        "message_hash": hashlib.sha256(scope.user_text.encode()).hexdigest(),
        "limits": scope.typed_input.get("limits", {}),
    }


def _filters(value: dict[str, Any]) -> ValidatedSourceFilters:
    return ValidatedSourceFilters(
        tuple(value.get("include_domains", ())),
        tuple(value.get("exclude_domains", ())),
        datetime.fromisoformat(value["published_after"]) if value.get("published_after") else None,
        datetime.fromisoformat(value["published_before"])
        if value.get("published_before")
        else None,
    )


def _parent_remaining(scope: RetrievalRunScope) -> dict[str, int]:
    remaining: dict[str, int] = {}
    for key, (maximum, used) in PARENT_KEYS.items():
        limit = scope.parent_budget.get(maximum)
        consumed = scope.parent_usage.get(used)
        if type(limit) is not int or type(consumed) is not int or limit < 0 or consumed < 0:
            raise RetrievalPolicyError("PARENT_BUDGET_UNAVAILABLE")
        remaining[key] = max(0, limit - consumed)
    return remaining


def _metadata(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) - {
        "safe_code",
        "retryable",
        "provider_requests",
        "input_tokens",
        "output_tokens",
        "cost_usd",
    }:
        raise RetrievalPolicyError("RETRIEVAL_METADATA_INVALID")
    code = value.get("safe_code")
    if code is not None and (
        not isinstance(code, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) is None
    ):
        raise RetrievalPolicyError("RETRIEVAL_METADATA_INVALID")
    for key in ("provider_requests", "input_tokens", "output_tokens"):
        amount = value.get(key)
        if amount is not None and (type(amount) is not int or amount < 0):
            raise RetrievalPolicyError("RETRIEVAL_METADATA_INVALID")
    if "retryable" in value and type(value["retryable"]) is not bool:
        raise RetrievalPolicyError("RETRIEVAL_METADATA_INVALID")
    result = dict(value)
    if value.get("cost_usd") is not None:
        try:
            cost = Decimal(str(value["cost_usd"]))
        except InvalidOperation:
            raise RetrievalPolicyError("RETRIEVAL_METADATA_INVALID") from None
        if not cost.is_finite() or cost < 0:
            raise RetrievalPolicyError("RETRIEVAL_METADATA_INVALID")
        result["cost_usd"] = str(cost)
    return result


class RetrievalBudgetService:
    def __init__(
        self, *, transactions: RetrievalTransactions, activation: RetrievalActivationPort
    ) -> None:
        self._transactions, self._activation = transactions, activation

    @asynccontextmanager
    async def _transaction(
        self, actor: AuthenticatedActor, run_id: UUID | None
    ) -> AsyncGenerator[RetrievalRepository]:
        try:
            self._activation.ensure_enabled()
            async with self._transactions(actor) as repository:
                yield repository
                self._activation.ensure_enabled()
        except RetrievalPolicyError as error:
            async with self._transactions(actor) as repository:
                await repository.audit("RETRIEVAL_REJECTED", run_id, code=str(error))
            raise

    async def start_run(
        self, actor: AuthenticatedActor, run_id: UUID, lease: RetrievalLease
    ) -> RetrievalBudgetState:
        async with self._transaction(actor, run_id) as repo:
            scope = await repo.bind(run_id, lease)
            state = await repo.get_budget(run_id)
            if state is not None:
                if scope.now >= min(state.deadline, scope.started_at + timedelta(seconds=25)):
                    raise RetrievalPolicyError("RETRIEVAL_DEADLINE_EXCEEDED")
                if state.lease != lease:
                    await repo.mark_unknown(run_id)
                    state = replace(
                        state, lease=lease, fencing_token=uuid4(), version=state.version + 1
                    )
                    await repo.save_budget(state)
                return state
            parent = _parent_remaining(scope)
            limits = scope.typed_input.get("limits", {})
            caps = dict(HARD_CAPS)
            limit_keys = {
                "queries": "max_queries",
                "page_reads": "max_page_reads",
                "provider_requests": "max_provider_requests",
                "nodes": "max_iterations",
            }
            for key in caps:
                declared = limits.get(limit_keys.get(key, f"max_{key}"), caps[key])
                if type(declared) is not int or declared < 0:
                    raise RetrievalPolicyError("RETRIEVAL_BUDGET_INVALID")
                caps[key] = min(caps[key], declared)
                if key in PARENT_KEYS:
                    caps[key] = min(
                        caps[key],
                        parent[key],
                        scope.handoff_budget.get(PARENT_KEYS[key][0], caps[key]),
                    )
            timeout = min(
                25,
                scope.handoff_budget.get("timeout_seconds", 25),
                limits.get("timeout_seconds", 25),
            )
            parent_timeout = scope.parent_budget.get("timeout_seconds")
            if type(parent_timeout) is not int:
                raise RetrievalPolicyError("PARENT_BUDGET_UNAVAILABLE")
            deadline = min(
                scope.started_at + timedelta(seconds=timeout),
                scope.parent_started_at + timedelta(seconds=parent_timeout),
                scope.lease_until or scope.now,
            )
            if deadline <= scope.now:
                raise RetrievalPolicyError("RETRIEVAL_DEADLINE_EXCEEDED")
            state = RetrievalBudgetState(
                actor.organization_id,
                run_id,
                actor.membership_id,
                scope.orchestration_run_id,
                lease,
                uuid4(),
                _public_scope(scope),
                caps,
                dict.fromkeys(HARD_CAPS, 0),
                scope.started_at,
                deadline,
                scope.started_at + timedelta(days=30),
            )
            await repo.insert_budget(state)
            await repo.audit("RETRIEVAL_RUN_STARTED", run_id)
            return state

    async def validate_run(
        self,
        actor: AuthenticatedActor,
        run_id: UUID,
        lease: RetrievalLease,
        *,
        allow_terminal: bool = False,
    ) -> None:
        async with self._transaction(actor, run_id) as repo:
            await self._state(repo, run_id, lease, allow_terminal=allow_terminal)

    async def _state(
        self,
        repo: RetrievalRepository,
        run_id: UUID,
        lease: RetrievalLease,
        *,
        allow_terminal: bool = False,
    ) -> tuple[RetrievalRunScope, RetrievalBudgetState]:
        scope = await repo.bind(run_id, lease, allow_terminal=allow_terminal)
        state = await repo.get_budget(run_id)
        if state is None or state.lease != lease:
            raise RetrievalPolicyError("RETRIEVAL_JOB_CLAIM_LOST")
        if scope.now >= min(state.deadline, scope.started_at + timedelta(seconds=25)):
            raise RetrievalPolicyError("RETRIEVAL_DEADLINE_EXCEEDED")
        return scope, state

    async def _request_allowed(
        self,
        repo: RetrievalRepository,
        state: RetrievalBudgetState,
        request: PublicSearchRequest | PublicPageRequest | None,
        kind: str,
    ) -> None:
        if kind == "SEARCH":
            if (
                not isinstance(request, PublicSearchRequest)
                or request_hash(
                    PublicSearchRequest(_normalized(request.query), request.validated_filters, 1)
                )
                not in state.scope["search_scope_hashes"]
            ):
                raise RetrievalPolicyError("PUBLIC_QUERY_PROVENANCE_REQUIRED")
            if request.limit > state.scope.get("limits", {}).get("results_per_query", 5):
                raise RetrievalPolicyError("RETRIEVAL_BUDGET_EXCEEDED")
        elif kind == "PAGE_READ":
            if not isinstance(request, PublicPageRequest) or (
                request_hash(request) not in state.scope["explicit_url_hashes"]
                and not await repo.source_url_allowed(state.agent_run_id, request.url.value)
            ):
                raise RetrievalPolicyError("PUBLIC_URL_PROVENANCE_REQUIRED")

    async def reserve_attempt(
        self,
        actor: AuthenticatedActor,
        agent_run_id: UUID,
        operation_key: str,
        attempt_no: int,
        kind: str,
        *,
        lease: RetrievalLease,
        request: PublicSearchRequest | PublicPageRequest | None = None,
        token_bounds: tuple[int, int] = (0, 0),
    ) -> RetrievalReservation:
        async with self._transaction(actor, agent_run_id) as repo:
            scope, state = await self._state(repo, agent_run_id, lease)
            await self._request_allowed(repo, state, request, kind)
            if (
                type(attempt_no) is not int
                or not 1 <= attempt_no <= 2
                or not 1 <= len(operation_key) <= 200
            ):
                raise RetrievalPolicyError("RETRIEVAL_OPERATION_INVALID")
            if await repo.has_unknown(agent_run_id) and kind in {"SEARCH", "PAGE_READ", "MODEL"}:
                raise RetrievalPolicyError("RETRIEVAL_RESULT_UNKNOWN")
            existing = await repo.get_attempt(agent_run_id, operation_key, attempt_no)
            if existing is not None:
                raise RetrievalPolicyError(
                    "RETRIEVAL_ATTEMPT_IN_PROGRESS"
                    if existing.status == "RESERVED"
                    else "RETRIEVAL_ATTEMPT_ALREADY_USED"
                )
            charge = charge_for(kind, token_bounds)
            limits = state.scope.get("limits", {})
            if kind == "MODEL" and token_bounds[1] > limits.get(
                "max_output_tokens_per_attempt", 1600
            ):
                raise RetrievalPolicyError("RETRIEVAL_TOKEN_BUDGET_EXCEEDED")
            if attempt_no == 2:
                previous = await repo.get_attempt(agent_run_id, operation_key, 1)
                if (
                    previous is None
                    or previous.status != "COMPLETED"
                    or not previous.metadata.get("retryable")
                    or previous.receipt.kind != kind
                    or previous.request_hash != request_hash(request)
                ):
                    raise RetrievalPolicyError("RETRIEVAL_RETRY_FORBIDDEN")
                charge["queries"] = charge["page_reads"] = 0
            if any(state.reserved[key] + charge[key] > state.caps[key] for key in HARD_CAPS):
                raise RetrievalPolicyError("RETRIEVAL_BUDGET_EXCEEDED")
            remaining = _parent_remaining(scope)
            if any(charge[key] > remaining[key] for key in PARENT_KEYS):
                raise RetrievalPolicyError("PARENT_BUDGET_EXCEEDED")
            state = replace(
                state,
                reserved={key: state.reserved[key] + charge[key] for key in HARD_CAPS},
                version=state.version + 1,
            )
            deadline = state.deadline
            if kind in {"SEARCH", "PAGE_READ"}:
                deadline = min(
                    deadline,
                    state.started_at
                    + timedelta(seconds=limits.get("retrieval_timeout_seconds", 8)),
                )
            elif kind == "MODEL":
                deadline = min(
                    deadline,
                    scope.now + timedelta(seconds=limits.get("synthesis_timeout_seconds", 20)),
                )
            if deadline <= scope.now:
                raise RetrievalPolicyError("RETRIEVAL_DEADLINE_EXCEEDED")
            receipt = RetrievalReservation(
                uuid4(),
                actor.organization_id,
                actor.membership_id,
                agent_run_id,
                operation_key,
                attempt_no,
                state.fencing_token,
                deadline,
                kind,
            )
            await repo.save_budget(state)
            await repo.charge_parent(scope, charge)
            await repo.insert_attempt(RetrievalAttempt(receipt, request_hash(request), charge))
            await repo.audit("RETRIEVAL_ATTEMPT_RESERVED", agent_run_id)
            return receipt

    async def _receipt(
        self,
        repo: RetrievalRepository,
        actor: AuthenticatedActor,
        receipt: RetrievalReservation,
        lease: RetrievalLease,
    ) -> tuple[RetrievalBudgetState, RetrievalAttempt]:
        scope, state = await self._state(repo, receipt.agent_run_id, lease)
        if receipt.deadline <= scope.now:
            raise RetrievalPolicyError("RETRIEVAL_DEADLINE_EXCEEDED")
        stored = await repo.get_attempt(
            receipt.agent_run_id, receipt.operation_key, receipt.attempt_no
        )
        if (
            stored is None
            or stored.receipt != receipt
            or state.fencing_token != receipt.fencing_token
            or actor.organization_id != receipt.organization_id
            or actor.membership_id != receipt.membership_id
            or stored.status != "RESERVED"
        ):
            raise RetrievalPolicyError("RETRIEVAL_RECEIPT_INVALID")
        return state, stored

    async def authorize(
        self,
        actor: AuthenticatedActor,
        reservation: RetrievalReservation,
        request: PublicSearchRequest | PublicPageRequest,
    ) -> None:
        async with self._transaction(actor, reservation.agent_run_id) as repo:
            # Read the claim identity without taking the budget lock. _receipt then
            # locks membership -> job -> parent -> agent -> budget, like reservations.
            state = await repo.get_budget(reservation.agent_run_id, lock=False)
            if state is None:
                raise RetrievalPolicyError("RETRIEVAL_RECEIPT_INVALID")
            state, stored = await self._receipt(repo, actor, reservation, state.lease)
            await self._request_allowed(repo, state, request, reservation.kind)
            if stored.request_hash != request_hash(request):
                raise RetrievalPolicyError("RETRIEVAL_RECEIPT_INVALID")
            await repo.dispatch(reservation)

    async def record_attempt_result(
        self,
        actor: AuthenticatedActor,
        receipt: RetrievalReservation,
        *,
        lease: RetrievalLease,
        metadata: dict[str, Any],
        sources: tuple[RetrievedSource, ...] = (),
        invocation_id: UUID | None = None,
    ) -> tuple[RetrievalSnapshot, ...]:
        async with self._transaction(actor, receipt.agent_run_id) as repo:
            state, stored = await self._receipt(repo, actor, receipt, lease)
            safe = _metadata(metadata)
            overrun = any(
                safe.get(key) is not None and safe[key] > stored.charged[key]
                for key in ("provider_requests", "input_tokens", "output_tokens")
            )
            if overrun:
                safe.update(safe_code="RETRIEVAL_OBSERVED_USAGE_EXCEEDED", retryable=False)
                sources = ()
            for source in sources:
                if (
                    validate_public_url(source.url.value) != source.url
                    or len(source.text.encode()) > 65536
                    or not 1 <= len(source.title) <= 500
                    or hashlib.sha256(source.text.encode()).hexdigest() != source.fingerprint
                ):
                    raise RetrievalPolicyError("RETRIEVAL_SOURCE_INVALID")
            if sources and (
                not stored.dispatched
                or invocation_id is None
                or receipt.kind not in {"SEARCH", "PAGE_READ"}
            ):
                raise RetrievalPolicyError("RETRIEVAL_RECEIPT_INVALID")
            snapshots = (
                await repo.store_sources(state, invocation_id, sources)
                if sources and invocation_id is not None
                else ()
            )
            await repo.finish_attempt(receipt, safe, unknown=overrun)
            await repo.audit(
                "RETRIEVAL_ATTEMPT_COMPLETED",
                receipt.agent_run_id,
                code="RETRIEVAL_OBSERVED_USAGE_EXCEEDED" if overrun else None,
            )
        if overrun:
            raise RetrievalPolicyError("RETRIEVAL_OBSERVED_USAGE_EXCEEDED")
        return snapshots

    async def load_evidence(
        self,
        actor: AuthenticatedActor,
        refs: tuple[UUID, ...],
        *,
        expected_hashes: tuple[str, ...],
        lease: RetrievalLease | None = None,
        expected_run_id: UUID | None = None,
        invocation_id: UUID | None = None,
    ) -> tuple[RetrievalSnapshot, ...]:
        async with self._transaction(actor, expected_run_id) as repo:
            if (
                not 1 <= len(refs) <= 8
                or len(set(refs)) != len(refs)
                or len(refs) != len(expected_hashes)
            ):
                raise RetrievalPolicyError("RETRIEVAL_SOURCE_UNAVAILABLE")
            sources = await repo.load_sources(refs)
            for source, fingerprint in zip(sources, expected_hashes, strict=True):
                scope = await repo.bind(source.agent_run_id, lease, allow_terminal=True)
                if (
                    source.source.fingerprint != fingerprint
                    or hashlib.sha256(source.source.text.encode()).hexdigest() != fingerprint
                    or source.expires_at <= scope.now
                    or (expected_run_id is not None and source.agent_run_id != expected_run_id)
                    or (invocation_id is not None and source.tool_invocation_id != invocation_id)
                ):
                    raise RetrievalPolicyError("RETRIEVAL_SOURCE_UNAVAILABLE")
                if lease is not None:
                    state = await repo.get_budget(source.agent_run_id)
                    if state is None or state.lease != lease or scope.now >= state.deadline:
                        raise RetrievalPolicyError("RETRIEVAL_JOB_CLAIM_LOST")
            await repo.audit("RETRIEVAL_EVIDENCE_REVALIDATED", expected_run_id)
            return sources

    async def remaining_seconds(
        self,
        actor: AuthenticatedActor,
        run_id: UUID,
        lease: RetrievalLease,
        *,
        retrieval: bool = False,
    ) -> float:
        await self.start_run(actor, run_id, lease)
        async with self._transaction(actor, run_id) as repo:
            scope, state = await self._state(repo, run_id, lease)
            deadline = state.deadline
            if retrieval:
                deadline = min(
                    deadline,
                    state.started_at
                    + timedelta(
                        seconds=state.scope.get("limits", {}).get("retrieval_timeout_seconds", 8)
                    ),
                )
            return max(0.0, (deadline - scope.now).total_seconds())

    async def load_checkpoint(
        self, actor: AuthenticatedActor, run_id: UUID, lease: RetrievalLease
    ) -> dict[str, Any] | None:
        await self.start_run(actor, run_id, lease)
        async with self._transaction(actor, run_id) as repo:
            await self._state(repo, run_id, lease)
            return await repo.load_checkpoint(run_id)

    async def save_checkpoint(
        self, actor: AuthenticatedActor, run_id: UUID, lease: RetrievalLease, value: dict[str, Any]
    ) -> None:
        async with self._transaction(actor, run_id) as repo:
            await self._state(repo, run_id, lease)
            await repo.save_checkpoint(run_id, value)

    async def validate_source_ids(self, actor: AuthenticatedActor, refs: tuple[UUID, ...]) -> None:
        async with self._transaction(actor, None) as repo:
            sources = await repo.load_sources(refs)
        await self.load_evidence(
            actor, refs, expected_hashes=tuple(source.source.fingerprint for source in sources)
        )

    async def validate_public_intent(
        self, actor: AuthenticatedActor, intent: dict[str, Any], message: str
    ) -> None:
        from app.modules.retrieval.application.query_policy import (
            validate_public_query as validate_intent,
        )

        async with self._transaction(actor, None) as repo:
            terms = await repo.protected_terms()
            validate_intent(intent, message, terms)
