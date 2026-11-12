"""Login counters never retain raw account/IP and have bounded outage state."""

from typing import Any
from uuid import uuid4

import pytest

from app.modules.optimization.application.admission import (
    AdmissionDecision,
    RateLimitRule,
    RateLimitUnavailable,
)
from app.modules.optimization.application.login_throttle import LoginThrottle

KEY = b"synthetic-login-throttle-key-only-32-bytes"


class Limiter:
    def __init__(self, unavailable: bool = False):
        self.unavailable = unavailable
        self.rules: tuple[RateLimitRule, ...] = ()

    async def check(self, rules: tuple[RateLimitRule, ...]) -> AdmissionDecision:
        self.rules = rules
        if self.unavailable:
            raise RateLimitUnavailable()
        return AdmissionDecision(True, 0)


@pytest.mark.asyncio
async def test_login_keys_are_hmac_normalized_and_tenant_scoped():
    limiter = Limiter()
    throttle = LoginThrottle(limiter, namespace="test:x", hash_key=KEY)
    org = uuid4()
    await throttle.check(
        trusted_organization_id=org,
        normalized_account=" User@Example.test ",
        client_ip="203.0.113.10",
    )
    first = limiter.rules
    assert [rule.limit for rule in first] == [10, 60]
    assert all(
        str(org) in rule.key
        and "user" not in rule.key.casefold()
        and "203.0.113.10" not in rule.key
        for rule in first
    )
    await throttle.check(
        trusted_organization_id=org,
        normalized_account="user@example.test",
        client_ip="203.0.113.10",
    )
    assert limiter.rules == first
    await throttle.check(
        trusted_organization_id=uuid4(),
        normalized_account="user@example.test",
        client_ip="203.0.113.10",
    )
    assert all(a.key != b.key for a, b in zip(first, limiter.rules, strict=True))


@pytest.mark.asyncio
async def test_account_11th_attempt_is_limited_during_redis_outage():
    throttle = LoginThrottle(Limiter(True), namespace="test:x", hash_key=KEY, clock=lambda: 100.0)
    values: dict[str, Any] = dict(
        trusted_organization_id=uuid4(),
        normalized_account="user@example.test",
        client_ip="203.0.113.10",
    )
    for _ in range(10):
        assert (await throttle.check(**values)).allowed
    denied = await throttle.check(**values)
    assert not denied.allowed and 1 <= denied.retry_after_seconds <= 60
    assert throttle.degraded_checks == 11


@pytest.mark.asyncio
async def test_ip_61st_attempt_is_limited_during_redis_outage():
    throttle = LoginThrottle(Limiter(True), namespace="test:x", hash_key=KEY, clock=lambda: 100.0)
    org = uuid4()
    for index in range(60):
        assert (
            await throttle.check(
                trusted_organization_id=org,
                normalized_account=f"user{index}@example.test",
                client_ip="203.0.113.10",
            )
        ).allowed
    assert not (
        await throttle.check(
            trusted_organization_id=org,
            normalized_account="next@example.test",
            client_ip="203.0.113.10",
        )
    ).allowed


@pytest.mark.asyncio
async def test_outage_fallback_remembers_healthy_attempts_and_expires():
    time = [100.0]
    limiter = Limiter()
    throttle = LoginThrottle(limiter, namespace="test:x", hash_key=KEY, clock=lambda: time[0])
    values: dict[str, Any] = dict(
        trusted_organization_id=uuid4(),
        normalized_account="user@example.test",
        client_ip="203.0.113.10",
    )
    for _ in range(10):
        assert (await throttle.check(**values)).allowed
    limiter.unavailable = True
    assert not (await throttle.check(**values)).allowed
    time[0] += 61
    assert (await throttle.check(**values)).allowed


@pytest.mark.asyncio
async def test_local_fallback_never_evicts_a_live_limit_to_accept_another_key():
    time = [100.0]
    throttle = LoginThrottle(
        Limiter(True), namespace="test:x", hash_key=KEY, clock=lambda: time[0], max_keys=4
    )
    org = uuid4()
    for index in range(3):
        assert (
            await throttle.check(
                trusted_organization_id=org,
                normalized_account=f"{index}@example.test",
                client_ip="203.0.113.10",
            )
        ).allowed
    assert throttle.fallback_size == 4
    assert not (
        await throttle.check(
            trusted_organization_id=org,
            normalized_account="new@example.test",
            client_ip="203.0.113.10",
        )
    ).allowed
    assert throttle.fallback_size == 4
    time[0] += 61
    assert (
        await throttle.check(
            trusted_organization_id=org,
            normalized_account="new@example.test",
            client_ip="203.0.113.10",
        )
    ).allowed
    assert throttle.fallback_size <= 4
