"""Real atomic counters on an explicitly supplied task-owned Redis instance."""

import asyncio
import json
import os
import sys
from typing import Protocol, cast
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.modules.optimization.adapters.redis_runtime import create_redis_runtime
from app.modules.optimization.application.admission import RateLimitRule, RateLimitUnavailable

pytestmark = [
    pytest.mark.integration,
    pytest.mark.redis_integration,
    pytest.mark.skipif(
        os.getenv("RUN_REDIS_INTEGRATION") != "1", reason="requires task-owned Redis"
    ),
]


class ClockProbe(Protocol):
    async def time(self) -> tuple[int, int]: ...


def runtime(namespace: str):
    return create_redis_runtime(
        Settings(
            environment="test",
            ai_admission_enabled=True,
            redis_url=SecretStr(os.environ["REDIS_TEST_URL"]),
            redis_namespace=namespace,
        )
    )


@pytest.mark.asyncio
async def test_actor_limit_is_atomic_across_independent_pools() -> None:
    one = runtime("limit-" + uuid4().hex[:20])
    two = runtime(one.namespace.split(":", 1)[1])
    assert one.limiter is not None and two.limiter is not None
    rules = (
        RateLimitRule(f"wm:{one.namespace}:limit:org:actor", 20, 60),
        RateLimitRule(f"wm:{one.namespace}:limit:org", 200, 60),
    )
    semaphore = asyncio.Semaphore(10)

    async def check(index: int):
        async with semaphore:
            limiter = one.limiter if index % 2 else two.limiter
            assert limiter is not None
            return await limiter.check(rules)

    try:
        results = await asyncio.gather(*(check(i) for i in range(40)))
        assert sum(result.allowed for result in results) == 20
        assert all(
            1 <= result.retry_after_seconds <= 60 for result in results if not result.allowed
        )
        assert one.client is not None
        value = await one.client.get(rules[1].key)
        assert isinstance(value, bytes) and value.endswith(b":20")
    finally:
        await one.aclose()
        await two.aclose()


@pytest.mark.asyncio
async def test_org_limit_is_shared_across_actors_without_consuming_denials() -> None:
    instance = runtime("org-" + uuid4().hex[:20])
    assert instance.limiter is not None
    prefix = f"wm:{instance.namespace}:limit:org"
    try:
        accepted = 0
        for i in range(201):
            decision = await instance.limiter.check(
                (RateLimitRule(f"{prefix}:actor:{i // 20}", 20, 60), RateLimitRule(prefix, 200, 60))
            )
            accepted += decision.allowed
        assert accepted == 200
        assert instance.client is not None
        assert await instance.client.get(f"{prefix}:actor:10") is None
    finally:
        await instance.aclose()


@pytest.mark.asyncio
async def test_malformed_counter_is_unavailable_before_either_counter_changes() -> None:
    instance = runtime("bad-" + uuid4().hex[:20])
    assert instance.limiter is not None and instance.client is not None
    prefix = f"wm:{instance.namespace}:limit:org"
    try:
        await instance.client.set(prefix, b"corrupt", ex=60)
        with pytest.raises(RateLimitUnavailable):
            await instance.limiter.check(
                (RateLimitRule(prefix + ":actor", 20, 60), RateLimitRule(prefix, 200, 60))
            )
        assert await instance.client.get(prefix + ":actor") is None
        assert await instance.client.get(prefix) == b"corrupt"
    finally:
        await instance.aclose()


@pytest.mark.asyncio
async def test_previous_window_counter_is_replaced_and_ttl_is_bounded() -> None:
    instance = runtime("window-" + uuid4().hex[:20])
    assert instance.client is not None and instance.limiter is not None
    key = f"wm:{instance.namespace}:limit:window"
    try:
        seconds, _ = await cast(ClockProbe, instance.client).time()
        await instance.client.set(key, f"{seconds // 60 - 1}:20", ex=60)
        assert (await instance.limiter.check((RateLimitRule(key, 20, 60),))).allowed
        value = await instance.client.get(key)
        assert isinstance(value, bytes) and value.endswith(b":1")
        assert 0 < await instance.client.pttl(key) <= 60_000
    finally:
        await instance.aclose()


@pytest.mark.asyncio
async def test_redis_oom_never_returns_confirmed_admission() -> None:
    if not os.environ.get("REDIS_OOM_TEST_URL"):
        pytest.skip("requires separate task-owned noeviction Redis")
    instance = create_redis_runtime(
        Settings(
            environment="test",
            ai_admission_enabled=True,
            redis_url=SecretStr(os.environ["REDIS_OOM_TEST_URL"]),
            redis_namespace="oom-" + uuid4().hex[:20],
        )
    )
    assert instance.limiter is not None
    try:
        with pytest.raises(RateLimitUnavailable):
            await instance.limiter.check(
                (RateLimitRule(f"wm:{instance.namespace}:limit:oom", 20, 60),)
            )
    finally:
        await instance.aclose()


@pytest.mark.asyncio
async def test_org_limit_is_shared_by_separate_api_processes() -> None:
    namespace = "process-" + uuid4().hex[:20]
    instance = runtime(namespace)
    try:
        assert instance.client is not None
        seconds, _ = await cast(ClockProbe, instance.client).time()
        remaining = 60 - seconds % 60
        if remaining < 5:
            await asyncio.sleep(remaining + 0.01)
        script = """
import asyncio, json, os, sys
from pydantic import SecretStr
from app.core.config import Settings
from app.modules.optimization.adapters.redis_runtime import create_redis_runtime
from app.modules.optimization.application.admission import RateLimitRule
async def main():
    runtime=create_redis_runtime(Settings(environment="test",ai_admission_enabled=True,redis_namespace=sys.argv[1],redis_url=SecretStr(os.environ["REDIS_TEST_URL"])))
    prefix=f"wm:{runtime.namespace}:limit:org"
    try:
        accepted=0
        for i in range(200):
            rules=(RateLimitRule(f"{prefix}:actor:{sys.argv[2]}:{i//20}",20,60),RateLimitRule(prefix,200,60))
            accepted+=(await runtime.limiter.check(rules)).allowed
        print(json.dumps({"accepted":accepted}))
    finally:
        await runtime.aclose()
asyncio.run(main())
"""

        async def run(index: int) -> int:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                script,
                namespace,
                str(index),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            assert process.returncode == 0, stderr.decode()[-500:]
            return int(json.loads(stdout)["accepted"])

        async with asyncio.timeout(20):
            assert sum(await asyncio.gather(run(1), run(2))) == 200
    finally:
        await instance.aclose()
