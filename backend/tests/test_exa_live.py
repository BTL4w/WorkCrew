"""Explicit, credential-gated smoke test. Never part of default or mock suites."""

import os
from decimal import Decimal

import pytest

from app.scripts.benchmark_retrieve_web import (
    BenchmarkBudget,
    default_queries,
    hosted_dependencies,
    run_benchmark,
)


@pytest.mark.live_provider
@pytest.mark.asyncio
async def test_opt_in_exa_search_respects_tiny_cap() -> None:
    if os.environ.get("RUN_EXA_LIVE") != "1" or not os.environ.get("APP_EXA_API_KEY"):
        pytest.skip("RUN_EXA_LIVE=1 and an injected Exa key are required")
    adapter, gateway, _ = hosted_dependencies("search-only")
    assert gateway is None
    report = await run_benchmark(
        default_queries()[:1],
        mode="search-only",
        adapter=adapter,
        budget=BenchmarkBudget(
            max_requests=1,
            retrieval_cap=Decimal("0.02"),
            model_cap=Decimal(0),
            total_cap=Decimal("0.02"),
        ),
        hosted=True,
    )
    assert report.attempted == 1 and report.completed == 1
    assert report.model_attempts == 0
    assert report.conservative_retrieval_usd <= Decimal("0.02")
    assert not report.hosted_ready
