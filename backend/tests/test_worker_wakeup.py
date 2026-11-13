"""Worker hints schedule scans of the configured SQL tenant allowlist only."""

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from app.modules.optimization.application.wakeup import SignalSubscription, WakeupHint, WakeupScope
from app.worker import worker_wakeups


class Hints:
    def __init__(self):
        self.scopes: list[WakeupScope] = []
        self.events: dict[WakeupScope, asyncio.Event] = {}
        self.open = 0

    async def publish(self, hint: WakeupHint) -> None:
        signal = self.events.get(hint.scope)
        if signal:
            signal.set()

    @asynccontextmanager
    async def subscribe(self, scope: WakeupScope):
        self.scopes.append(scope)
        self.open += 1
        try:
            yield SignalSubscription(self.events.setdefault(scope, asyncio.Event()))
        finally:
            self.open -= 1


@pytest.mark.asyncio
async def test_worker_subscribes_to_trusted_allowlist_before_scan_and_cleans_up():
    org, foreign = uuid4(), uuid4()
    hints = Hints()
    async with worker_wakeups(hints, [org, org]) as wake:
        assert hints.scopes == [WakeupScope(org, "job.ready", org)]
        await hints.publish(WakeupHint(1, foreign, "job.ready", foreign, None))
        assert not any(signal.is_set() for signal in hints.events.values())
        await hints.publish(WakeupHint(1, org, "job.ready", org, None))
        async with asyncio.timeout(0.2):
            await wake.wait(timeout_seconds=15)
    assert hints.open == 0


@pytest.mark.asyncio
async def test_worker_without_hints_waits_for_existing_poll_deadline():
    async with worker_wakeups(None, [uuid4()]) as wake:
        clock = asyncio.get_running_loop().time
        before = clock()
        await wake.wait(timeout_seconds=0.02)
        assert clock() - before >= 0.015


@pytest.mark.asyncio
async def test_worker_wait_cancellation_closes_child_tasks_and_subscriptions():
    hints = Hints()
    async with worker_wakeups(hints, [uuid4(), uuid4()]) as wake:
        task = asyncio.create_task(wake.wait(timeout_seconds=15))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert hints.open == 0
    assert not [task for task in asyncio.all_tasks() if task.get_name() == "worker-wakeup-wait"]


@pytest.mark.asyncio
async def test_worker_subscriptions_are_bounded_with_overflow_polling():
    hints = Hints()
    async with worker_wakeups(hints, [uuid4() for _ in range(1002)]):
        assert len(hints.scopes) == 1000
    assert hints.open == 0
