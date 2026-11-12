"""Shared provider admission follows current access and public egress checks."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.optimization.application.admission import (
    AdmissionDecision,
    RateLimitRule,
    RateLimitUnavailable,
)
from app.modules.retrieval.application.admission import RetrievalAdmissionService
from app.modules.retrieval.application.service import RetrievalService
from app.modules.retrieval.domain.models import (
    PublicPageRequest,
    PublicSearchRequest,
    RetrievalPolicyError,
    RetrievalProviderError,
    RetrievalReservation,
)
from app.modules.retrieval.domain.public_urls import validate_public_url
from tests.test_ai_admission import actor as _actor
from tests.test_retrieval_service import DNS, Access, NeverProvider


def _receipt(actor: AuthenticatedActor, kind: str = "SEARCH") -> RetrievalReservation:
    return RetrievalReservation(
        id=uuid4(),
        organization_id=actor.organization_id,
        membership_id=actor.membership_id,
        agent_run_id=uuid4(),
        operation_key="search",
        attempt_no=1,
        fencing_token=uuid4(),
        deadline=datetime.now(UTC) + timedelta(seconds=5),
        kind=kind,
    )


class Limiter:
    def __init__(self, unavailable: bool = False):
        self.calls = 0
        self.rules: tuple[RateLimitRule, ...] = ()
        self.unavailable = unavailable

    async def check(self, rules: tuple[RateLimitRule, ...]) -> AdmissionDecision:
        self.calls += 1
        self.rules = rules
        if self.unavailable:
            raise RateLimitUnavailable()
        return AdmissionDecision(False, 30)


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", [False, True])
@pytest.mark.parametrize("kind", ["SEARCH", "PAGE_READ"])
async def test_denied_attempt_has_zero_provider_requests_after_current_authorization(
    unavailable: bool,
    kind: str,
):
    actor = _actor()
    access = Access()
    limiter = Limiter(unavailable)
    admission = RetrievalAdmissionService(limiter, namespace="test:x")
    provider = NeverProvider()
    service = RetrievalService(
        search=provider, pages=provider, access=access, dns=DNS(), admission=admission
    )
    with pytest.raises(RetrievalProviderError) as error:
        if kind == "SEARCH":
            await service.search(actor, PublicSearchRequest("public planning"), _receipt(actor))
        else:
            await service.read_page(
                actor,
                PublicPageRequest(validate_public_url("https://example.org/guide")),
                _receipt(actor, kind),
            )
    assert access.calls == limiter.calls == 1
    assert error.value.provider_requests == 0
    assert not error.value.retryable
    assert error.value.safe_code == (
        "RETRIEVAL_ADMISSION_UNAVAILABLE" if unavailable else "RETRIEVAL_RATE_LIMITED"
    )
    assert [rule.limit for rule in limiter.rules] == [30, 300]
    assert all(
        str(actor.organization_id) in rule.key and ":limit:retrieval:" in rule.key
        for rule in limiter.rules
    )


@pytest.mark.asyncio
async def test_disabled_capability_never_uses_limiter_or_provider():
    actor = _actor()
    limiter = Limiter()
    provider = NeverProvider()
    service = RetrievalService(
        search=provider,
        pages=provider,
        access=Access(reject=True),
        dns=DNS(),
        admission=RetrievalAdmissionService(limiter, namespace="test:x"),
    )
    with pytest.raises(RetrievalPolicyError):
        await service.search(actor, PublicSearchRequest("public planning"), _receipt(actor))
    assert limiter.calls == 0


@pytest.mark.asyncio
async def test_private_dns_denial_does_not_consume_retrieval_admission():
    actor = _actor()
    limiter = Limiter()
    provider = NeverProvider()
    service = RetrievalService(
        search=provider,
        pages=provider,
        access=Access(),
        dns=DNS(("127.0.0.1",)),
        admission=RetrievalAdmissionService(limiter, namespace="test:x"),
    )
    with pytest.raises(RetrievalPolicyError, match="UNSAFE_PUBLIC_URL"):
        await service.read_page(
            actor,
            PublicPageRequest(validate_public_url("https://example.com/")),
            _receipt(actor, "PAGE_READ"),
        )
    assert limiter.calls == 0
