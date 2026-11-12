"""Application admission is tenant-scoped and fails closed for new AI work."""

import asyncio
from uuid import uuid4

import pytest

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.optimization.application.admission import (
    AdmissionDecision,
    AdmissionUnavailable,
    AIAdmissionService,
    RateLimited,
    RateLimitRule,
    RateLimitUnavailable,
)
from app.modules.organization.domain.roles import MembershipRole


def actor() -> AuthenticatedActor:
    return AuthenticatedActor(
        user_id=uuid4(),
        email="synthetic@example.test",
        display_name="Synthetic",
        membership_id=uuid4(),
        organization_id=uuid4(),
        organization_name="Synthetic",
        role=MembershipRole.EMPLOYEE,
    )


class Limiter:
    def __init__(self, decision: AdmissionDecision | Exception):
        self.decision = decision
        self.rules: tuple[RateLimitRule, ...] = ()

    async def check(self, rules: tuple[RateLimitRule, ...]) -> AdmissionDecision:
        self.rules = rules
        if isinstance(self.decision, BaseException):
            raise self.decision
        return self.decision


@pytest.mark.asyncio
async def test_ai_admission_uses_current_tenant_actor_and_separate_org_rule() -> None:
    limiter = Limiter(AdmissionDecision(True, 0))
    subject = actor()
    await AIAdmissionService(limiter, namespace="test:workcrew").require_new_work(subject)
    actor_rule, org_rule = limiter.rules
    assert (actor_rule.limit, org_rule.limit) == (20, 200)
    assert actor_rule.window_seconds == org_rule.window_seconds == 60
    assert (
        str(subject.organization_id) in actor_rule.key
        and str(subject.membership_id) in actor_rule.key
    )
    assert (
        str(subject.organization_id) in org_rule.key
        and str(subject.membership_id) not in org_rule.key
    )
    assert subject.email not in actor_rule.key


@pytest.mark.asyncio
async def test_denial_preserves_bounded_retry_hint() -> None:
    with pytest.raises(RateLimited) as error:
        await AIAdmissionService(
            Limiter(AdmissionDecision(False, 17)), namespace="test:x"
        ).require_new_work(actor())
    assert error.value.code == "RATE_LIMITED"
    assert error.value.retry_after_seconds == 17


@pytest.mark.asyncio
async def test_backend_failure_cannot_admit_new_work() -> None:
    with pytest.raises(AdmissionUnavailable) as error:
        await AIAdmissionService(
            Limiter(RateLimitUnavailable()), namespace="test:x"
        ).require_new_work(actor())
    assert error.value.retry_after_seconds == 5


@pytest.mark.asyncio
async def test_caller_cancellation_is_not_a_retryable_admission_error() -> None:
    class Cancelled:
        async def check(self, rules: tuple[RateLimitRule, ...]) -> AdmissionDecision:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await AIAdmissionService(Cancelled(), namespace="test:x").require_new_work(actor())


@pytest.mark.parametrize("limit,window", [(0, 60), (-1, 60), (1, 0), (1, 61), (True, 60)])
def test_rules_reject_invalid_bounds(limit: int, window: int) -> None:
    with pytest.raises(ValueError):
        RateLimitRule("wm:test:x", limit, window)


@pytest.mark.asyncio
async def test_limiter_deadline_and_malformed_reply_fail_closed() -> None:
    from typing import cast

    from redis.asyncio import Redis

    from app.modules.optimization.adapters.redis_limiter import RedisRateLimiter

    class Slow:
        async def eval(self, *args: object) -> object:
            await asyncio.sleep(10)
            return [1, 0]

    limiter = RedisRateLimiter(cast(Redis, Slow()), "test:x")
    async with asyncio.timeout(0.2):
        with pytest.raises(RateLimitUnavailable):
            await limiter.check((RateLimitRule("wm:test:x:limit:org", 20, 60),))

    class Malformed:
        async def eval(self, *args: object) -> object:
            return [1, 10]

    limiter = RedisRateLimiter(cast(Redis, Malformed()), "test:x")
    with pytest.raises(RateLimitUnavailable):
        await limiter.check((RateLimitRule("wm:test:x:limit:org", 20, 60),))


@pytest.mark.asyncio
async def test_accepted_card_replay_does_not_revalidate_source_for_new_work() -> None:
    from app.modules.assistant.application.service import AssistantService
    from tests.test_assistant_service import (
        FakeRepository,
        FakeTransactionFactory,
        FixedPlanningSnapshot,
    )

    subject = actor()
    limiter = Limiter(AdmissionDecision(True, 0))
    snapshot = FixedPlanningSnapshot(1)
    service = AssistantService(
        transaction_factory=FakeTransactionFactory(FakeRepository()),
        planning_snapshot=snapshot,
        orchestrator_version="1.0.0",
        orchestrator_fingerprint="test",
        admission=AIAdmissionService(limiter, namespace="test:x"),
    )
    conversation = await service.create_conversation(
        actor=subject, locale="en", title=None, request_id="test", idempotency_key=uuid4().hex
    )
    key = uuid4().hex

    async def send():
        return await service.post_message(
            actor=subject,
            conversation_id=conversation.conversation.id,
            message="Revise the plan",
            locale="en",
            card_action={"kind": "PLANNING_REVISE", "proposal_id": str(subject.user_id)},
            if_match_version=1,
            request_id="test",
            idempotency_key=key,
        )

    first = await send()
    snapshot.version = 2
    limiter.decision = RateLimitUnavailable()
    replay = await send()
    assert replay.replayed and replay.job.id == first.job.id


@pytest.mark.asyncio
async def test_admission_rejection_cannot_silently_lose_required_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import Any

    from app.modules.assistant.application.service import AssistantService, AssistantServiceError
    from tests.test_assistant_service import (
        FakeRepository,
        FakeTransactionFactory,
        NullPlanningSnapshot,
    )

    repo = FakeRepository()

    async def unavailable_audit(**_: Any) -> None:
        raise RuntimeError("audit storage unavailable")

    monkeypatch.setattr(repo, "append_rejected_audit", unavailable_audit)
    subject = actor()
    service = AssistantService(
        transaction_factory=FakeTransactionFactory(repo),
        planning_snapshot=NullPlanningSnapshot(),
        orchestrator_version="1.0.0",
        orchestrator_fingerprint="test",
        admission=AIAdmissionService(Limiter(AdmissionDecision(False, 5)), namespace="test:x"),
    )
    conversation = await service.create_conversation(
        actor=subject, locale="en", title=None, request_id="test", idempotency_key=uuid4().hex
    )
    with pytest.raises(AssistantServiceError) as error:
        await service.post_message(
            actor=subject,
            conversation_id=conversation.conversation.id,
            message="New work",
            locale="en",
            card_action=None,
            if_match_version=None,
            request_id="test",
            idempotency_key=uuid4().hex,
        )
    assert error.value.code == "AUDIT_UNAVAILABLE"
    assert repo.jobs == []
