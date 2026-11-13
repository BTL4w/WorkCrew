"""Bounded real Redis Pub/Sub multiplexing with validated coalesced signals."""

import asyncio
import os
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.modules.optimization.adapters.redis_runtime import create_redis_runtime
from app.modules.optimization.application.wakeup import PollingSubscription, WakeupHint, WakeupScope

pytestmark = [
    pytest.mark.integration,
    pytest.mark.redis_integration,
    pytest.mark.skipif(os.getenv("RUN_REDIS_INTEGRATION") != "1", reason="requires owned Redis"),
]


@pytest.mark.asyncio
async def test_pubsub_validates_channel_and_coalesces_resource_signal():
    runtime = create_redis_runtime(
        Settings(
            environment="test",
            redis_wakeup_enabled=True,
            redis_url=SecretStr(os.environ["REDIS_TEST_URL"]),
            redis_namespace="wake-" + uuid4().hex[:20],
        )
    )
    assert runtime.wakeup is not None
    org, resource = uuid4(), uuid4()
    scope = WakeupScope(org, "assistant.events", resource)
    try:
        async with runtime.wakeup.subscribe(scope) as subscription:
            # Drain the subscribe/reconnect catch-up signal before the actual publish.
            await subscription.wait(timeout_seconds=0.3)
            for sequence in (3, 1, 3, 2):
                await runtime.wakeup.publish(
                    WakeupHint(1, org, "assistant.events", resource, sequence)
                )
            async with asyncio.timeout(1):
                await subscription.wait(timeout_seconds=2)
        assert runtime.wakeup.active_scopes == 0
        assert runtime.wakeup.subscriber_connections <= 2
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_malformed_and_wrong_tenant_hints_are_ignored_before_a_barrier():
    runtime = create_redis_runtime(
        Settings(
            environment="test",
            redis_wakeup_enabled=True,
            redis_url=SecretStr(os.environ["REDIS_TEST_URL"]),
            redis_namespace="bad-hint-" + uuid4().hex[:18],
        )
    )
    wake = runtime.wakeup
    assert wake is not None and runtime.client is not None
    scope = WakeupScope(uuid4(), "assistant.events", uuid4())
    client = cast(Any, runtime.client)
    try:
        async with wake.subscribe(scope) as subscription:
            await subscription.wait(timeout_seconds=0.2)
            await client.publish(wake.channel(scope), b'{"payload":"private"}')
            await client.publish(
                wake.channel(scope),
                WakeupHint(1, uuid4(), "assistant.events", scope.resource_id, 1).to_bytes(),
            )
            await client.publish(
                wake.channel(scope),
                WakeupHint(
                    1, scope.organization_id, "assistant.events", scope.resource_id, 3
                ).to_bytes(),
            )
            async with asyncio.timeout(1):
                await subscription.wait(timeout_seconds=2)
            assert wake.malformed_hints == 2 and wake.received_signals == 1
            await client.publish(
                wake.channel(scope),
                WakeupHint(
                    1, scope.organization_id, "assistant.events", scope.resource_id, 2
                ).to_bytes(),
            )
            await client.publish(
                wake.channel(scope),
                WakeupHint(
                    1, scope.organization_id, "assistant.events", scope.resource_id, 3
                ).to_bytes(),
            )
            await client.publish(
                wake.channel(scope),
                WakeupHint(
                    1, scope.organization_id, "assistant.events", scope.resource_id, 4
                ).to_bytes(),
            )
            async with asyncio.timeout(1):
                await subscription.wait(timeout_seconds=2)
            assert wake.received_signals == 2
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_scope_capacity_falls_back_and_shared_scope_cleanup_is_bounded():
    runtime = create_redis_runtime(
        Settings(
            environment="test",
            redis_wakeup_enabled=True,
            redis_url=SecretStr(os.environ["REDIS_TEST_URL"]),
            redis_namespace="caps-" + uuid4().hex[:20],
        )
    )
    wake = runtime.wakeup
    assert wake is not None
    cast(Any, wake)._max_scopes = 1
    scope = WakeupScope(uuid4(), "assistant.events", uuid4())
    try:
        async with wake.subscribe(scope):
            async with (
                wake.subscribe(scope),
                wake.subscribe(WakeupScope(uuid4(), "assistant.events", uuid4())) as overflow,
            ):
                assert isinstance(overflow, PollingSubscription)
                assert wake.active_scopes == 1 and wake.subscriber_connections <= 2
            assert wake.active_scopes == 1
        assert wake.active_scopes == 0
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_subscriber_disconnect_resubscribes_and_forces_sql_catchup():
    runtime = create_redis_runtime(
        Settings(
            environment="test",
            redis_wakeup_enabled=True,
            redis_url=SecretStr(os.environ["REDIS_TEST_URL"]),
            redis_namespace="reconnect-" + uuid4().hex[:18],
        )
    )
    wake = runtime.wakeup
    assert wake is not None
    scope = WakeupScope(uuid4(), "assistant.events", uuid4())
    try:
        async with wake.subscribe(scope) as subscription:
            await subscription.wait(timeout_seconds=0.2)
            before = wake.subscription_acks
            await cast(Any, wake)._subscriber.connection_pool.disconnect(inuse_connections=True)
            async with asyncio.timeout(2):
                while wake.subscription_acks == before:  # noqa: ASYNC110 -- observable SDK reconnect barrier
                    await asyncio.sleep(0.005)
            await subscription.wait(timeout_seconds=0.2)
            await wake.publish(
                WakeupHint(1, scope.organization_id, "assistant.events", scope.resource_id, 1)
            )
            async with asyncio.timeout(1):
                await subscription.wait(timeout_seconds=2)
    finally:
        await runtime.aclose()
