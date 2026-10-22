"""Required authorization, DNS and provider contracts; none supply authority by default."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.retrieval.domain.models import (
    PageReadResult,
    PublicPageRequest,
    PublicSearchRequest,
    RetrievalReservation,
    RetrievedSource,
    SearchBatch,
)
from app.modules.retrieval.domain.state import (
    RetrievalAttempt,
    RetrievalBudgetState,
    RetrievalLease,
    RetrievalRunScope,
    RetrievalSnapshot,
)


class RetrievalActivationPort(Protocol):
    def ensure_enabled(self) -> None: ...


class RetrievalRepository(Protocol):
    async def bind(
        self, run_id: UUID, lease: RetrievalLease | None, *, allow_terminal: bool = False
    ) -> RetrievalRunScope: ...
    async def get_budget(
        self, run_id: UUID, *, lock: bool = True
    ) -> RetrievalBudgetState | None: ...
    async def insert_budget(self, state: RetrievalBudgetState) -> None: ...
    async def save_budget(self, state: RetrievalBudgetState) -> None: ...
    async def charge_parent(self, scope: RetrievalRunScope, charge: dict[str, int]) -> None: ...
    async def get_attempt(
        self, run_id: UUID, operation: str, attempt: int
    ) -> RetrievalAttempt | None: ...
    async def insert_attempt(self, attempt: RetrievalAttempt) -> None: ...
    async def dispatch(self, receipt: RetrievalReservation) -> None: ...
    async def finish_attempt(
        self, receipt: RetrievalReservation, metadata: dict[str, Any], *, unknown: bool = False
    ) -> None: ...
    async def mark_unknown(self, run_id: UUID) -> None: ...
    async def has_unknown(self, run_id: UUID) -> bool: ...
    async def store_sources(
        self, state: RetrievalBudgetState, invocation_id: UUID, sources: tuple[RetrievedSource, ...]
    ) -> tuple[RetrievalSnapshot, ...]: ...
    async def load_sources(self, refs: tuple[UUID, ...]) -> tuple[RetrievalSnapshot, ...]: ...
    async def source_url_allowed(self, run_id: UUID, url: str) -> bool: ...
    async def audit(self, action: str, run_id: UUID | None, *, code: str | None = None) -> None: ...


RetrievalTransactions = Callable[
    [AuthenticatedActor], AbstractAsyncContextManager[RetrievalRepository]
]


class WebSearchPort(Protocol):
    async def search(
        self, request: PublicSearchRequest, *, timeout_seconds: float
    ) -> SearchBatch: ...


class PublicPageReadPort(Protocol):
    async def read(
        self, request: PublicPageRequest, *, timeout_seconds: float
    ) -> PageReadResult: ...


class PublicDNSResolver(Protocol):
    async def resolve(self, hostname: str) -> tuple[str, ...]: ...


class RetrievalAccessPort(Protocol):
    async def authorize(
        self,
        actor: AuthenticatedActor,
        reservation: RetrievalReservation,
        request: PublicSearchRequest | PublicPageRequest,
    ) -> None:
        """Recheck current membership, Core/I3/provider gates and durable reservation.

        Bind the exact operation/query/filters or selected URL to trusted provenance,
        parent budget, run, attempt, fencing token and deadline before public egress.
        Reject revoked actors, replayed/unknown receipts and private context. Domain
        DTOs and opaque identifiers are never proof of permission. Implemented with
        durable state in Task 3; no permissive implementation is composed here.
        """
        ...
