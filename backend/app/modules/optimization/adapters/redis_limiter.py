"""One bounded Lua invocation decides all rules atomically on shared Redis."""

import asyncio
from pathlib import Path
from typing import cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.modules.optimization.application.admission import (
    AdmissionDecision,
    RateLimitRule,
    RateLimitUnavailable,
)

_SCRIPT = (Path(__file__).parent / "scripts" / "fixed_window.lua").read_text(encoding="utf-8")


class RedisRateLimiter:
    def __init__(self, client: Redis, namespace: str):
        self._client = client
        self._prefix = f"wm:{namespace}:limit:"

    async def check(self, rules: tuple[RateLimitRule, ...]) -> AdmissionDecision:
        if not 1 <= len(rules) <= 4 or any(not r.key.startswith(self._prefix) for r in rules):
            raise RateLimitUnavailable()
        keys = [r.key for r in rules]
        args = [str(value) for r in rules for value in (r.limit, r.window_seconds)]
        try:
            async with asyncio.timeout(0.05):
                reply = await self._client.eval(_SCRIPT, len(keys), *keys, *args)
        except (RedisError, OSError, TimeoutError) as exc:
            # Do not expose provider messages, key contents or credentials.
            raise RateLimitUnavailable() from exc
        if not isinstance(reply, list):
            raise RateLimitUnavailable()
        values = cast(list[object], reply)
        if len(values) != 2:
            raise RateLimitUnavailable()
        allowed, retry = values
        if type(allowed) is not int or type(retry) is not int:
            raise RateLimitUnavailable()
        if not ((allowed == 1 and retry == 0) or (allowed == 0 and 1 <= retry <= 60)):
            raise RateLimitUnavailable()
        return AdmissionDecision(allowed == 1, retry)
