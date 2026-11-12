"""Separate shared throttle for each public retrieval provider attempt."""

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.optimization.application.admission import (
    RateLimitPort,
    RateLimitRule,
    RateLimitUnavailable,
)
from app.modules.retrieval.domain.models import RetrievalProviderError


class RetrievalAdmissionService:
    def __init__(
        self, limiter: RateLimitPort, *, namespace: str, actor_limit: int = 30, org_limit: int = 300
    ):
        self._limiter = limiter
        self._namespace = namespace
        self._actor_limit = actor_limit
        self._org_limit = org_limit

    async def require_provider_call(self, actor: AuthenticatedActor) -> None:
        prefix = f"wm:{self._namespace}:limit:retrieval:org:{actor.organization_id}"
        try:
            decision = await self._limiter.check(
                (
                    RateLimitRule(f"{prefix}:actor:{actor.membership_id}", self._actor_limit, 60),
                    RateLimitRule(prefix, self._org_limit, 60),
                )
            )
        except RateLimitUnavailable:
            raise RetrievalProviderError(
                "RETRIEVAL_ADMISSION_UNAVAILABLE", provider_requests=0
            ) from None
        if not decision.allowed:
            raise RetrievalProviderError("RETRIEVAL_RATE_LIMITED", provider_requests=0)
