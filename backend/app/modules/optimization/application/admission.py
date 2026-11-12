"""Shared admission for new work; durable authorization and budgets stay in SQL."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from app.modules.identity.domain.auth import AuthenticatedActor

NewWorkGuard = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    key: str
    limit: int
    window_seconds: int

    def __post_init__(self) -> None:
        if (
            not self.key.startswith("wm:")
            or len(self.key) > 512
            or type(self.limit) is not int
            or not 1 <= self.limit <= 1_000_000
            or type(self.window_seconds) is not int
            or not 1 <= self.window_seconds <= 60
        ):
            raise ValueError("INVALID_RATE_LIMIT_RULE")


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    allowed: bool
    retry_after_seconds: int


class RateLimitUnavailable(Exception):
    """No confirmed atomic decision; callers must never infer admission."""


class RateLimitPort(Protocol):
    async def check(self, rules: tuple[RateLimitRule, ...]) -> AdmissionDecision: ...


class AdmissionError(Exception):
    def __init__(self, code: str, status: int, retry_after_seconds: int):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after_seconds = retry_after_seconds


class RateLimited(AdmissionError):
    def __init__(self, retry_after_seconds: int):
        super().__init__("RATE_LIMITED", 429, max(1, min(60, retry_after_seconds)))


class AdmissionUnavailable(AdmissionError):
    def __init__(self):
        super().__init__("AI_ADMISSION_UNAVAILABLE", 503, 5)


class AIAdmissionService:
    def __init__(
        self, limiter: RateLimitPort, *, namespace: str, actor_limit: int = 20, org_limit: int = 200
    ):
        self._limiter = limiter
        self._namespace = namespace
        self._actor_limit = actor_limit
        self._org_limit = org_limit

    async def require_new_work(self, actor: AuthenticatedActor) -> None:
        prefix = f"wm:{self._namespace}:limit:ai:org:{actor.organization_id}"
        rules = (
            RateLimitRule(f"{prefix}:actor:{actor.membership_id}", self._actor_limit, 60),
            RateLimitRule(prefix, self._org_limit, 60),
        )
        try:
            decision = await self._limiter.check(rules)
        except RateLimitUnavailable as exc:
            raise AdmissionUnavailable() from exc
        if not decision.allowed:
            raise RateLimited(decision.retry_after_seconds)
