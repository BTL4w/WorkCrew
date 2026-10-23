"""Required web coverage fails closed; mock regression passes never promote hosted quality."""

import pytest


@pytest.mark.asyncio
async def test_bilingual_required_cases_execute_real_graphs_and_safe_contracts():
    from work_management_ai.evaluation.retrieve_web import load_cases, run_retrieve_web_suite

    report = await run_retrieve_web_suite(load_cases())
    required = {(f"W{i:02}", locale) for i in (*range(1, 14), 18) for locale in ("vi", "en")}
    rows = {(row.case_id, row.locale): row for row in report.rows}
    assert required <= rows.keys()
    for key in required:
        row = rows[key]
        assert row.passed, row.model_dump()
        assert row.orchestrator_runs >= 1
        assert row.assertions and all(row.assertions.values())
        if row.actual_route == "retrieve":
            assert row.retrieve_runs == 1
    assert report.passed
    assert report.required_total == 28
    assert report.safety_violations == 0
    assert report.hosted_status == "NOT_RUN" and not report.hosted_ready
    assert report.held_out_routing_accuracy is None
    assert report.semantic_support_accuracy is None
    assert {(r.case_id, r.locale) for r in report.rows if r.status == "DEFERRED_I2"} == {
        (f"W{i:02}", locale) for i in (14, 15, 17) for locale in ("vi", "en")
    }


@pytest.mark.asyncio
async def test_missing_required_row_or_wrong_label_fails_the_gate():
    from work_management_ai.evaluation.retrieve_web import load_cases, run_retrieve_web_suite

    cases = load_cases()
    assert not (await run_retrieve_web_suite(cases[1:])).passed
    wrong = tuple(
        case.model_copy(update={"expected_outcome": "ANSWER_READY"})
        if case.case_id == "W04"
        else case
        for case in cases
    )
    report = await run_retrieve_web_suite(wrong)
    assert not report.passed
    assert all(not row.passed for row in report.rows if row.case_id == "W04")


@pytest.mark.asyncio
async def test_evidence_only_failure_and_non_web_routes_do_not_add_model_or_search_work():
    from work_management_ai.evaluation.retrieve_web import load_cases, run_retrieve_web_suite

    report = await run_retrieve_web_suite(load_cases())
    for row in report.rows:
        if row.case_id == "W03":
            assert row.synthesis_attempts == 0
        if row.case_id in {
            "W08",
            "W09",
            "W13",
            "W18",
            "W18_REVISE",
            "W18_DEADLINE",
            "BOTH_INACTIVE",
        }:
            assert row.tool_calls == 0
        if row.case_id == "W10":
            assert row.actual_outcome == "EVIDENCE_READY" and row.claim_count == 0
        if row.case_id == "W16":
            assert row.actual_route == "retrieve"


@pytest.mark.asyncio
async def test_skipping_a_required_assertion_fails_even_when_other_assertions_pass():
    from work_management_ai.evaluation.retrieve_web import load_cases, run_retrieve_web_suite

    report = await run_retrieve_web_suite(load_cases())
    row = next(row for row in report.rows if row.case_id == "W02")
    assert row.passed
    assert not row.model_copy(
        update={
            "assertions": {
                key: value for key, value in row.assertions.items() if key != "page_read"
            }
        }
    ).passed


@pytest.mark.asyncio
async def test_mock_suite_rejects_a_non_mock_gateway_before_execution():
    from typing import cast

    from work_management_ai.evaluation.retrieve_web import load_cases, run_retrieve_web_suite
    from work_management_ai.model_gateway.contracts import ModelGateway

    with pytest.raises(ValueError, match="MOCK_GATEWAY_REQUIRED"):
        await run_retrieve_web_suite(load_cases(), gateway=cast(ModelGateway, object()))


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["published_at", "source_fetched_at"])
async def test_invented_source_dates_fail_required_unknown_freshness_gate(field: str) -> None:
    from datetime import UTC, datetime

    from work_management_ai.evaluation.retrieve_web import (
        Case,
        EvaluationTools,
        load_cases,
        run_retrieve_web_suite,
    )
    from work_management_ai.runtime.contracts import ActorReference

    class InventedDates(EvaluationTools):
        def __init__(self, case: Case, actor: ActorReference) -> None:
            super().__init__(case, actor)
            if case.scenario == "conflict":
                self.sources = tuple(
                    source.model_copy(update={field: datetime.now(UTC)}) for source in self.sources
                )
                self.by_id = {source.evidence_id: source for source in self.sources}

    report = await run_retrieve_web_suite(load_cases(), tools=InventedDates)
    assert not report.passed
    conflict = [row for row in report.rows if row.case_id == "W12"]
    assert len(conflict) == 2
    assert all(not row.assertions["conflict_and_unknown_freshness"] for row in conflict)
