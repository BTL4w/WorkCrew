"""HMAC login counters with bounded process-local fallback during Redis outages."""

import hashlib
import hmac
import ipaddress
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from uuid import UUID

from app.modules.optimization.application.admission import (
    AdmissionDecision,
    RateLimitPort,
    RateLimitRule,
    RateLimitUnavailable,
)

logger = logging.getLogger(__name__)


class LoginThrottled(Exception):
    def __init__(self, retry_after_seconds: int):
        super().__init__("LOGIN_RATE_LIMITED")
        self.retry_after_seconds = max(1, min(60, retry_after_seconds))


class LoginThrottle:
    def __init__(
        self,
        limiter: RateLimitPort,
        *,
        namespace: str,
        hash_key: bytes,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 4096,
    ):
        if len(hash_key) < 32 or not 2 <= max_keys <= 100_000:
            raise ValueError("INVALID_LOGIN_THROTTLE_CONFIGURATION")
        self._limiter = limiter
        self._namespace = namespace
        self._hash_key = hash_key
        self._clock = clock
        self._max_keys = max_keys
        self._counts: OrderedDict[str, int] = OrderedDict()
        self._bucket: int | None = None
        self._warned_bucket: int | None = None
        self.degraded_checks = 0

    @property
    def fallback_size(self) -> int:
        return len(self._counts)

    def _digest(self, org: UUID, family: str, value: str) -> str:
        return hmac.new(
            self._hash_key, f"{org}\0{family}\0{value}".encode(), hashlib.sha256
        ).hexdigest()

    def _local_check(self, rules: tuple[RateLimitRule, ...]) -> AdmissionDecision:
        now = self._clock()
        bucket = int(now // 60)
        retry = 60 - int(now % 60)
        if bucket != self._bucket:
            self._counts.clear()
            self._bucket = bucket
        # Live limits must not be evicted to make room for attacker-chosen accounts.
        if len(self._counts) + sum(rule.key not in self._counts for rule in rules) > self._max_keys:
            return AdmissionDecision(False, retry)
        if any(self._counts.get(rule.key, 0) >= rule.limit for rule in rules):
            return AdmissionDecision(False, retry)
        for rule in rules:
            self._counts[rule.key] = self._counts.get(rule.key, 0) + 1
            self._counts.move_to_end(rule.key)
        return AdmissionDecision(True, 0)

    async def check(
        self, *, trusted_organization_id: UUID, normalized_account: str, client_ip: str
    ) -> AdmissionDecision:
        account = normalized_account.strip().casefold()
        try:
            peer = ipaddress.ip_address(client_ip).compressed
        except ValueError:
            peer = "unknown"
        prefix = f"wm:{self._namespace}:limit:login:org:{trusted_organization_id}"
        rules = (
            RateLimitRule(
                f"{prefix}:account:{self._digest(trusted_organization_id, 'account', account)}",
                10,
                60,
            ),
            RateLimitRule(
                f"{prefix}:ip:{self._digest(trusted_organization_id, 'ip', peer)}", 60, 60
            ),
        )
        # Shadow healthy attempts so this process cannot get an empty outage counter.
        local = self._local_check(rules)
        try:
            return await self._limiter.check(rules)
        except RateLimitUnavailable:
            self.degraded_checks += 1
            if self._warned_bucket != self._bucket:
                logger.warning("login_throttle_redis_unavailable")
                self._warned_bucket = self._bucket
            return local
