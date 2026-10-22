"""Service refuses unverified receipts, private queries and unsafe DNS before provider I/O."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole

if TYPE_CHECKING:
    from app.modules.retrieval.domain.models import (
        PageReadResult,
        PublicPageRequest,
        PublicSearchRequest,
        RetrievalReservation,
        SearchBatch,
    )


def _actor() -> AuthenticatedActor:
    return AuthenticatedActor(
        user_id=uuid4(),
        email="employee@example.test",
        display_name="Employee",
        membership_id=uuid4(),
        organization_id=uuid4(),
        organization_name="Synthetic",
        role=MembershipRole.EMPLOYEE,
    )


def _receipt(actor: AuthenticatedActor, kind: str = "SEARCH") -> RetrievalReservation:
    from app.modules.retrieval.domain.models import RetrievalReservation

    return RetrievalReservation(
        id=uuid4(),
        organization_id=actor.organization_id,
        membership_id=actor.membership_id,
        agent_run_id=uuid4(),
        operation_key="search-1",
        attempt_no=1,
        fencing_token=uuid4(),
        deadline=datetime.now(UTC) + timedelta(seconds=5),
        kind=kind,
    )


class Access:
    """Explicit test-only trusted verifier, standing in for Task 3 persistence."""

    def __init__(self, reject: bool = False) -> None:
        self.reject = reject
        self.calls = 0

    async def authorize(
        self,
        actor: AuthenticatedActor,
        reservation: RetrievalReservation,
        request: PublicSearchRequest | PublicPageRequest,
    ) -> None:
        from app.modules.retrieval.domain.models import RetrievalPolicyError

        self.calls += 1
        if self.reject:
            raise RetrievalPolicyError("RETRIEVAL_ACCESS_DENIED")


class DNS:
    def __init__(self, answers: tuple[str, ...] = ("93.184.216.34",)) -> None:
        self.answers = answers
        self.calls = 0

    async def resolve(self, hostname: str) -> tuple[str, ...]:
        self.calls += 1
        return self.answers


class NeverProvider:
    async def search(self, request: PublicSearchRequest, *, timeout_seconds: float) -> SearchBatch:
        raise AssertionError("provider called before authorization/input checks")

    async def read(self, request: PublicPageRequest, *, timeout_seconds: float) -> PageReadResult:
        raise AssertionError("provider called before authorization/input checks")


@pytest.mark.asyncio
async def test_mock_and_exa_ports_require_an_explicit_access_verifier() -> None:
    from app.modules.retrieval.adapters.mock import MockRetrievalAdapter
    from app.modules.retrieval.application.service import RetrievalService
    from app.modules.retrieval.domain.models import PublicSearchRequest

    actor = _actor()
    adapter = MockRetrievalAdapter()
    access = Access()
    service = RetrievalService(search=adapter, pages=adapter, access=access, dns=DNS())
    batch = await service.search(actor, PublicSearchRequest("public planning"), _receipt(actor))
    assert access.calls == 1
    assert len(batch.sources) == 1
    assert batch.provider_usage.requests == 0
    with pytest.raises(TypeError):
        RetrievalService(search=adapter, pages=adapter, dns=DNS())  # type: ignore[call-arg]


@pytest.mark.parametrize("change", ["tenant", "membership", "expired", "kind"])
@pytest.mark.asyncio
async def test_invalid_receipt_is_rejected_before_provider_io(change: str) -> None:
    from app.modules.retrieval.application.service import RetrievalService
    from app.modules.retrieval.domain.models import PublicSearchRequest, RetrievalPolicyError

    actor = _actor()
    receipt = _receipt(actor)
    if change == "tenant":
        receipt = replace(receipt, organization_id=uuid4())
    elif change == "membership":
        receipt = replace(receipt, membership_id=uuid4())
    elif change == "expired":
        receipt = replace(receipt, deadline=datetime.now(UTC) - timedelta(seconds=1))
    else:
        receipt = replace(receipt, kind="PAGE_READ")

    access = Access()
    never = NeverProvider()
    service = RetrievalService(search=never, pages=never, access=access, dns=DNS())
    with pytest.raises(RetrievalPolicyError):
        await service.search(actor, PublicSearchRequest("public"), receipt)
    assert access.calls == 0


@pytest.mark.asyncio
async def test_authoritative_access_denial_precedes_dns_or_exa() -> None:
    from app.modules.retrieval.application.service import RetrievalService
    from app.modules.retrieval.domain.models import PublicPageRequest, RetrievalPolicyError
    from app.modules.retrieval.domain.public_urls import validate_public_url

    actor = _actor()
    dns = DNS()
    adapter = NeverProvider()
    service = RetrievalService(search=adapter, pages=adapter, access=Access(reject=True), dns=dns)
    with pytest.raises(RetrievalPolicyError, match="ACCESS_DENIED"):
        await service.read_page(
            actor,
            PublicPageRequest(validate_public_url("https://example.org/guide")),
            _receipt(actor, "PAGE_READ"),
        )
    assert dns.calls == 0


@pytest.mark.asyncio
async def test_private_dns_answer_rejects_before_provider_request() -> None:
    from app.modules.retrieval.application.service import RetrievalService
    from app.modules.retrieval.domain.models import PublicPageRequest, RetrievalPolicyError
    from app.modules.retrieval.domain.public_urls import validate_public_url

    actor = _actor()
    adapter = NeverProvider()
    service = RetrievalService(
        search=adapter, pages=adapter, access=Access(), dns=DNS(("10.0.0.1",))
    )
    with pytest.raises(RetrievalPolicyError, match="UNSAFE_PUBLIC_URL"):
        await service.read_page(
            actor,
            PublicPageRequest(validate_public_url("https://public-looking.org/guide")),
            _receipt(actor, "PAGE_READ"),
        )


@pytest.mark.parametrize(
    "query",
    [
        "private user@company.test",
        "api_key=secret-value",
        "Bearer abcdef1234567890",
        "sk-proj-abcdefghijklmnop",
        "https://example.org/?token=private",
        "x" * 501,
        "   ",
        "invalid\ud800query",
    ],
)
def test_public_query_rejects_obvious_secrets_and_oversized_input(query: str) -> None:
    from app.modules.retrieval.domain.models import PublicSearchRequest

    with pytest.raises(ValueError, match="UNSAFE_PUBLIC_QUERY"):
        PublicSearchRequest(query)


@pytest.mark.parametrize(
    "locale_query", ["Hướng dẫn lập kế hoạch công khai", "Public planning guide"]
)
@pytest.mark.asyncio
async def test_bilingual_public_search_does_not_require_model_credentials(
    locale_query: str,
) -> None:
    from app.modules.retrieval.adapters.mock import MockRetrievalAdapter
    from app.modules.retrieval.application.service import RetrievalService
    from app.modules.retrieval.domain.models import PublicSearchRequest

    actor = _actor()
    adapter = MockRetrievalAdapter()
    service = RetrievalService(search=adapter, pages=adapter, access=Access(), dns=DNS())
    result = await service.search(actor, PublicSearchRequest(locale_query), _receipt(actor))
    assert result.sources[0].method == "SEARCH_SNIPPET"


@pytest.mark.asyncio
async def test_authorization_timeout_reports_no_provider_request() -> None:
    from app.modules.retrieval.application.service import RetrievalService
    from app.modules.retrieval.domain.models import PublicSearchRequest, RetrievalProviderError

    class WaitingAccess(Access):
        async def authorize(
            self,
            actor: AuthenticatedActor,
            reservation: RetrievalReservation,
            request: PublicSearchRequest | PublicPageRequest,
        ) -> None:
            await asyncio.Event().wait()

    actor = _actor()
    receipt = replace(_receipt(actor), deadline=datetime.now(UTC) + timedelta(seconds=0.5))
    adapter = NeverProvider()
    service = RetrievalService(search=adapter, pages=adapter, access=WaitingAccess(), dns=DNS())
    with pytest.raises(RetrievalProviderError, match="RETRIEVAL_TIMEOUT") as error:
        await service.search(actor, PublicSearchRequest("public"), receipt)
    assert error.value.provider_requests == 0


@pytest.mark.asyncio
async def test_cancellation_during_dns_stops_before_provider_io() -> None:
    from app.modules.retrieval.application.service import RetrievalService
    from app.modules.retrieval.domain.models import PublicPageRequest
    from app.modules.retrieval.domain.public_urls import validate_public_url

    started = asyncio.Event()

    class WaitingDNS(DNS):
        async def resolve(self, hostname: str) -> tuple[str, ...]:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("cancelled lookup resumed")

    actor = _actor()
    adapter = NeverProvider()
    service = RetrievalService(search=adapter, pages=adapter, access=Access(), dns=WaitingDNS())
    task = asyncio.create_task(
        service.read_page(
            actor,
            PublicPageRequest(validate_public_url("https://example.org/guide")),
            _receipt(actor, "PAGE_READ"),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
