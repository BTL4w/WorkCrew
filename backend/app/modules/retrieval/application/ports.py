"""Required authorization, DNS and provider contracts; none supply authority by default."""

from typing import Protocol

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.retrieval.domain.models import (
    PageReadResult,
    PublicPageRequest,
    PublicSearchRequest,
    RetrievalReservation,
    SearchBatch,
)


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
