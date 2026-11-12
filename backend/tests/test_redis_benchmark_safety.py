"""A benchmark must never seed a shared database or emit its credentials."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://user:secret@localhost:5432/work_management",
        "postgresql+psycopg://user:secret@production:55435/wm_redis_bench_0123456789ab",
        "postgresql+psycopg://user:secret@127.0.0.1:55435/work_management_e2e",
        "postgresql+psycopg://user:secret@127.0.0.1:55435/wm_redis_bench_ffffffffffff",
        "sqlite:///wm_redis_bench_0123456789ab",
    ],
)
def test_rejects_non_dedicated_database(url: str) -> None:
    from app.scripts.benchmark_redis import BenchmarkSafetyError, validate_target

    with pytest.raises(BenchmarkSafetyError, match="DEDICATED_DATABASE_REQUIRED"):
        validate_target(url, "0123456789abcdef0123456789abcdef")


def test_seed_requires_explicit_flag() -> None:
    from app.scripts.benchmark_redis import BenchmarkSafetyError, require_fixture_setup

    with pytest.raises(BenchmarkSafetyError, match="FIXTURE_SETUP_NOT_AUTHORIZED"):
        require_fixture_setup(False)
    require_fixture_setup(True)


def test_output_has_no_secrets() -> None:
    from app.scripts.benchmark_redis import FixtureSpec, build_report, validate_target

    token = "0123456789abcdef0123456789abcdef"
    target = validate_target(
        "postgresql+psycopg://user:private-password@127.0.0.1:55435/wm_redis_bench_0123456789ab",
        token,
    )
    report = build_report(target=target, fixture=FixtureSpec(), revision="test", series=[])
    encoded = json.dumps(report)
    assert "private-password" not in encoded
    assert "postgresql" not in encoded
    assert token not in encoded
    assert "private-password" not in repr(target)
    assert report["mode"] == "baseline"


def test_compare_refuses_different_dataset() -> None:
    from app.scripts.benchmark_redis import (
        BenchmarkSafetyError,
        FixtureSpec,
        build_report,
        validate_comparison,
        validate_target,
    )

    target = validate_target(
        "postgresql+psycopg://user:secret@127.0.0.1:55435/wm_redis_bench_0123456789ab",
        "0123456789abcdef0123456789abcdef",
    )
    baseline = build_report(target=target, fixture=FixtureSpec(), revision="a", series=[])
    current = build_report(
        target=target, fixture=FixtureSpec(tasks_per_tenant=100), revision="b", series=[]
    )
    with pytest.raises(BenchmarkSafetyError, match="DATASET_MISMATCH"):
        validate_comparison(baseline, current)
    validate_comparison(baseline, baseline)


def test_latency_summary_retains_failures_and_real_sql_cost() -> None:
    from app.scripts.benchmark_redis import Sample, summarize

    summary = summarize([Sample(10, 2, 3, 200), Sample(20, 4, 5, 200), Sample(30, 6, 7, 503)])
    assert summary["samples"] == 3
    assert summary["errors"] == 1
    assert summary["p50_ms"] == 20
    assert summary["p95_ms"] == 30
    assert summary["sql_statements_per_request"] == 4
    assert summary["sql_ms_per_request"] == 5


def test_compare_refuses_mismatch_before_database_or_fixture_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.scripts.benchmark_redis import main

    baseline = tmp_path / "baseline.json"
    baseline.write_text('{"dataset_fingerprint":"different"}')
    monkeypatch.setenv(
        "APP_DATABASE_URL",
        "postgresql+psycopg://user:secret@127.0.0.1:1/wm_redis_bench_0123456789ab",
    )
    monkeypatch.setenv("REDIS_BENCHMARK_INSTANCE_TOKEN", "0123456789abcdef0123456789abcdef")
    output = tmp_path / "result.json"
    assert (
        main(
            [
                "--mode",
                "compare",
                "--baseline",
                str(baseline),
                "--output",
                str(output),
                "--allow-fixture-setup",
            ]
        )
        == 2
    )
    assert capsys.readouterr().out.strip() == "DATASET_MISMATCH"
    assert not output.exists()
