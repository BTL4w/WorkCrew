"""Benchmark caps reserve before I/O, and offline/default modes never use credentials."""

from decimal import Decimal
from pathlib import Path

import pytest

from app.modules.retrieval.domain.models import PublicSearchRequest, SearchBatch


def test_budget_reserves_all_cost_dimensions_before_dispatch():
    from app.scripts.benchmark_retrieve_web import BenchmarkBudget, BudgetStop

    budget = BenchmarkBudget(
        max_requests=3, retrieval_cap=Decimal("1"), model_cap=Decimal("1"), total_cap=Decimal("1.5")
    )
    receipt = budget.reserve("search", Decimal("0.75"))
    budget.finish(receipt, None)
    assert budget.charged_retrieval == Decimal("0.75")
    with pytest.raises(BudgetStop):
        budget.reserve("search", Decimal("0.26"))
    other = BenchmarkBudget(
        max_requests=3, retrieval_cap=Decimal("1"), model_cap=Decimal("1"), total_cap=Decimal("1.5")
    )
    other.finish(other.reserve("search", Decimal("0.75")), None)
    with pytest.raises(BudgetStop):
        other.reserve("model", Decimal("0.76"))
    model_budget = BenchmarkBudget(max_requests=3)
    model = model_budget.reserve("model", Decimal("0.5"))
    model_budget.finish(model, Decimal("0.1"))
    assert model_budget.charged_model == Decimal("0.1")
    assert model_budget.request_count == 1


@pytest.mark.asyncio
async def test_search_benchmark_caps_calls_reports_unknown_cost_and_does_not_call_model():
    from app.modules.retrieval.adapters.mock import MockRetrievalAdapter
    from app.scripts.benchmark_retrieve_web import BenchmarkBudget, default_queries, run_benchmark

    budget = BenchmarkBudget(
        max_requests=2, retrieval_cap=Decimal("1"), model_cap=Decimal("0"), total_cap=Decimal("1")
    )
    report = await run_benchmark(
        default_queries(),
        mode="search-only",
        adapter=MockRetrievalAdapter(),
        budget=budget,
        hosted=False,
    )
    assert report.attempted == 2 and report.completed == 2
    assert report.stopped_reason == "REQUEST_CAP"
    assert report.model_attempts == 0
    assert not report.hosted_ready
    assert report.mode == "MOCK"
    assert len(default_queries()) == 80
    assert len({query.query.casefold() for query in default_queries()}) == 80


def test_cli_default_no_opt_in_does_not_construct_settings_or_adapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.scripts import benchmark_retrieve_web as cli

    def forbidden(*args: object, **kwargs: object):
        raise AssertionError("credentials or network accessed without opt-in")

    monkeypatch.setattr(cli, "hosted_dependencies", forbidden)
    output = tmp_path / "report.json"
    assert cli.main(["--output", str(output)]) == 0
    text = output.read_text()
    assert '"NOT_RUN"' in text
    assert "api_key" not in text and "raw_error" not in text


@pytest.mark.asyncio
async def test_timeout_and_provider_error_are_charged_conservatively_and_redacted():
    from app.modules.retrieval.adapters.mock import MockRetrievalAdapter
    from app.scripts.benchmark_retrieve_web import BenchmarkBudget, default_queries, run_benchmark

    class Failing(MockRetrievalAdapter):
        async def search(
            self, request: PublicSearchRequest, *, timeout_seconds: float
        ) -> SearchBatch:
            raise RuntimeError("sk-secret-provider-error-body")

    budget = BenchmarkBudget(
        max_requests=1, retrieval_cap=Decimal("1"), model_cap=Decimal("0"), total_cap=Decimal("1")
    )
    report = await run_benchmark(
        default_queries(), mode="search-only", adapter=Failing(), budget=budget, hosted=False
    )
    assert report.failed == 1 and report.completed == 0
    assert report.observed_retrieval_usd is None
    assert budget.charged_retrieval > 0
    assert "sk-secret" not in report.model_dump_json()


def test_existing_artifact_rejected_before_hosted_dependency_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.scripts import benchmark_retrieve_web as cli

    output = tmp_path / "report.json"
    output.write_text("existing immutable measurement")
    marker = tmp_path / "credentials-accessed"

    def forbidden(*args: object, **kwargs: object):
        marker.touch()
        raise RuntimeError("credentials were accessed")

    monkeypatch.setattr(cli, "hosted_dependencies", forbidden)
    assert cli.main(["--confirm-hosted", "--output", str(output)]) == 2
    assert not marker.exists()
    assert output.read_text() == "existing immutable measurement"


def test_once_stopped_the_budget_cannot_dispatch_a_cheaper_call():
    from app.scripts.benchmark_retrieve_web import BenchmarkBudget, BudgetStop

    budget = BenchmarkBudget(retrieval_cap=Decimal("0.01"))
    with pytest.raises(BudgetStop):
        budget.reserve("search", Decimal("0.02"))
    with pytest.raises(BudgetStop):
        budget.reserve("page", Decimal("0.001"))
    assert budget.request_count == 0


def test_journal_failure_stops_all_subsequent_dispatch():
    from app.scripts.benchmark_retrieve_web import BenchmarkBudget, BudgetStop

    def broken(event: dict[str, object]) -> None:
        raise OSError("private filesystem detail")

    budget = BenchmarkBudget(journal=broken)
    with pytest.raises(BudgetStop, match="JOURNAL_UNAVAILABLE"):
        budget.reserve("search", Decimal("0.012"))
    budget.journal = None
    with pytest.raises(BudgetStop, match="JOURNAL_UNAVAILABLE"):
        budget.reserve("search", Decimal("0.012"))


@pytest.mark.asyncio
async def test_end_to_end_mock_uses_real_graph_and_reserves_synthesis():
    from app.modules.retrieval.adapters.mock import MockRetrievalAdapter
    from app.scripts.benchmark_retrieve_web import (
        BenchmarkBudget,
        MockBenchmarkGateway,
        default_queries,
        run_benchmark,
    )

    budget = BenchmarkBudget(max_requests=6)
    report = await run_benchmark(
        default_queries()[:1],
        mode="end-to-end",
        adapter=MockRetrievalAdapter(),
        budget=budget,
        hosted=False,
        gateway=MockBenchmarkGateway(),
        model="gpt-4o-mini",
    )
    assert report.completed == 1
    assert report.model_attempts == 1
    assert report.rows[0].citation_bindings
    assert report.conservative_model_usd > 0
    assert report.latency_ms["synthesis"] is not None
    assert report.latency_ms["queue"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("has_sources", "safe_code", "warnings", "verified", "outcome", "completed"),
    [
        (False, "NOT_FOUND", (), True, "NOT_FOUND", 1),
        (False, "SOURCE_UNAVAILABLE", ("INVALID_SOURCE_URL",), True, "SOURCE_UNAVAILABLE", 0),
        (False, None, ("PROVIDER_NOTICE",), True, "SOURCE_UNAVAILABLE", 0),
        (False, "NOT_FOUND", (), False, "SOURCE_UNAVAILABLE", 0),
        (True, None, ("INVALID_SOURCE_CONTENT",), True, "PARTIAL", 1),
        (True, None, (), False, "PARTIAL", 1),
    ],
)
async def test_search_batch_status_is_preserved_in_measurement_denominators(
    has_sources: bool,
    safe_code: str | None,
    warnings: tuple[str, ...],
    verified: bool,
    outcome: str,
    completed: int,
) -> None:
    from dataclasses import replace

    from app.modules.retrieval.adapters.mock import MockRetrievalAdapter
    from app.scripts.benchmark_retrieve_web import BenchmarkBudget, default_queries, run_benchmark

    class BatchAdapter(MockRetrievalAdapter):
        async def search(
            self, request: PublicSearchRequest, *, timeout_seconds: float
        ) -> SearchBatch:
            batch = await super().search(request, timeout_seconds=timeout_seconds)
            return replace(
                batch,
                sources=batch.sources if has_sources else (),
                safe_code=safe_code,
                warnings=warnings,
                filters_verified=verified,
            )

    report = await run_benchmark(
        default_queries()[:1],
        mode="search-only",
        adapter=BatchAdapter(),
        budget=BenchmarkBudget(max_requests=1),
        hosted=False,
    )
    assert report.rows[0].outcome == outcome
    assert report.completed == completed
    assert report.failed == 1 - completed
