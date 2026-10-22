"""Read-only public retrieval after mandatory authorization and egress validation."""

import asyncio
import ipaddress
from datetime import UTC, datetime

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.retrieval.application.ports import (
    PublicDNSResolver,
    PublicPageReadPort,
    RetrievalAccessPort,
    WebSearchPort,
)
from app.modules.retrieval.domain.models import (
    PageReadResult,
    PublicPageRequest,
    PublicSearchRequest,
    RetrievalPolicyError,
    RetrievalProviderError,
    RetrievalReservation,
    SearchBatch,
)
from app.modules.retrieval.domain.public_urls import validate_public_addresses


class RetrievalService:
    def __init__(
        self,
        *,
        search: WebSearchPort,
        pages: PublicPageReadPort,
        access: RetrievalAccessPort,
        dns: PublicDNSResolver,
    ) -> None:
        self._search = search
        self._pages = pages
        self._access = access
        self._dns = dns

    @staticmethod
    def _remaining(actor: AuthenticatedActor, receipt: RetrievalReservation, kind: str) -> float:
        if (
            actor.organization_id != receipt.organization_id
            or actor.membership_id != receipt.membership_id
            or receipt.kind != kind
        ):
            raise RetrievalPolicyError("RETRIEVAL_RESERVATION_SCOPE_MISMATCH")
        remaining = (receipt.deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise RetrievalPolicyError("RETRIEVAL_DEADLINE_EXCEEDED")
        return min(8.0, remaining)

    async def search(
        self,
        actor: AuthenticatedActor,
        request: PublicSearchRequest,
        budget: RetrievalReservation,
    ) -> SearchBatch:
        timeout = self._remaining(actor, budget, "SEARCH")
        # Reconstruct typed input so mutated/fabricated dataclasses cannot bypass validation.
        request = PublicSearchRequest(request.query, request.validated_filters, request.limit)
        provider_started = False
        try:
            async with asyncio.timeout(timeout):
                await self._access.authorize(actor, budget, request)
                remaining = self._remaining(actor, budget, "SEARCH")
                provider_started = True
                return await self._search.search(request, timeout_seconds=remaining)
        except TimeoutError:
            raise RetrievalProviderError(
                "RETRIEVAL_TIMEOUT", retryable=True, provider_requests=int(provider_started)
            ) from None

    async def read_page(
        self,
        actor: AuthenticatedActor,
        request: PublicPageRequest,
        budget: RetrievalReservation,
    ) -> PageReadResult:
        timeout = self._remaining(actor, budget, "PAGE_READ")
        request = PublicPageRequest(request.url)
        provider_started = False
        try:
            async with asyncio.timeout(timeout):
                await self._access.authorize(actor, budget, request)
                try:
                    ipaddress.ip_address(request.url.hostname)
                except ValueError:
                    addresses = await self._dns.resolve(request.url.hostname)
                else:
                    addresses = (request.url.hostname,)
                try:
                    validate_public_addresses(addresses)
                except ValueError:
                    raise RetrievalPolicyError("UNSAFE_PUBLIC_URL") from None
                remaining = self._remaining(actor, budget, "PAGE_READ")
                provider_started = True
                return await self._pages.read(request, timeout_seconds=remaining)
        except TimeoutError:
            raise RetrievalProviderError(
                "RETRIEVAL_TIMEOUT", retryable=True, provider_requests=int(provider_started)
            ) from None
