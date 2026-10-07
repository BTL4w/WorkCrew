from typing import Any, Literal, cast
from uuid import uuid4

import pytest

from work_management_ai.agents.reporting.harness import ReportingHarness
from work_management_ai.evaluation.phase5_cases import load_cases
from work_management_ai.evaluation.phase5_reporting import (
    MockReportingEvaluationGateway,
    evaluate_gate,
    run_reporting_suite,
)
from work_management_ai.runtime.contracts import AgentHandoff, AgentResult, ToolExecutionRequest


@pytest.mark.asyncio
async def test_default_eval_uses_synthetic_mock():
    cases = load_cases()
    suite = await run_reporting_suite(cases, MockReportingEvaluationGateway())
    assert suite.gate.passed
    assert suite.passed == len(cases) and suite.failed == suite.skipped == 0
    assert {case.locale for case in cases} == {"vi", "en"}
    assert suite.gate.bindings_checked > 0 and suite.gate.refs_checked > 0
    assert suite.gate.coverage == suite.gate.required_coverage
    assert suite.hosted_quality == "NOT_RUN"
    assert all(
        item.model_refs == () or all(ref.startswith("mock:") for ref in item.model_refs)
        for item in suite.results
    )


@pytest.mark.asyncio
async def test_unexercised_policy_dimension_is_not_pass():
    suite = await run_reporting_suite((load_cases()[0],), MockReportingEvaluationGateway())
    assert not suite.gate.passed
    assert suite.gate.missing_coverage
    assert not evaluate_gate(()).passed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["foreign_reference", "forbidden_tool", "peer_handoff", "approval"]
)
async def test_measured_gate_fails_on_mutated_execution(
    mutation: Literal["foreign_reference", "forbidden_tool", "peer_handoff", "approval"],
):
    suite = await run_reporting_suite(
        load_cases(), MockReportingEvaluationGateway(), execution=UnsafeExecution(mutation)
    )
    assert not suite.gate.passed
    counters = {
        "foreign_reference": suite.gate.cross_tenant_leakage_count,
        "forbidden_tool": suite.gate.forbidden_tool_count,
        "peer_handoff": suite.gate.peer_handoff_count,
        "approval": suite.gate.approval_bypass_count,
    }
    assert counters[mutation] > 0


class UnsafeExecution:
    def __init__(
        self, mutation: Literal["foreign_reference", "forbidden_tool", "peer_handoff", "approval"]
    ):
        self.mutation = mutation

    async def run(self, harness: ReportingHarness, handoff: AgentHandoff) -> AgentResult:
        result = await harness.run(handoff)
        if not result.proposed_actions:
            return result
        if self.mutation == "forbidden_tool":
            await harness.tools.execute(
                ToolExecutionRequest(
                    agent_run_id=uuid4(),
                    tool_id="reporting.publish",
                    tool_version="1.0.0",
                    call_id="unsafe",
                    actor=handoff.actor,
                    typed_input={},
                    idempotency_key="unsafe-call-0001",
                )
            )
        elif self.mutation == "approval":
            result = result.model_copy(
                update={
                    "proposed_actions": tuple(
                        action.model_copy(update={"requires_human_gate": False})
                        for action in result.proposed_actions
                    )
                }
            )
        elif self.mutation == "peer_handoff":
            from work_management_ai.runtime.contracts import RequestedHandoff

            peer = RequestedHandoff(
                target_capability="planning.propose", objective="Unsafe", typed_input={}
            )
            result = result.model_copy(update={"requested_handoff": peer})
        else:
            output: dict[str, Any] = dict(result.typed_output)
            doc = output.get("narrative")
            if isinstance(doc, dict):
                for block in cast(list[dict[str, Any]], doc["blocks"]):
                    if block["kind"] != "FACT" and block["source_refs"]:
                        block["source_refs"][0]["resource_id"] = str(uuid4())
            result = result.model_copy(update={"typed_output": output})
        return result


@pytest.mark.asyncio
async def test_hosted_rubric_is_advisory_and_records_real_judge_call():
    from typing import Any

    from work_management_ai.agents.reporting.contracts import ReportingContext
    from work_management_ai.evaluation.phase5_cases import ReportingEvalCase
    from work_management_ai.model_gateway.mock import MockModelGateway

    class Judged:
        provider = "hosted"
        qualitative_judging = True

        def for_case(
            self,
            case: ReportingEvalCase,
            context: ReportingContext,
            document: dict[str, Any],
            semantic: dict[str, Any],
        ):
            return MockModelGateway(
                fixtures={
                    f"reporting.{case.locale}.draft": document,
                    f"reporting.{case.locale}.grounding": semantic,
                    "reporting.eval.rubric": {
                        "groundedness": 0,
                        "relevance": 1,
                        "assumptions": 2,
                        "usefulness": 1,
                        "safe_codes": [],
                    },
                },
                model_ref="mock:advisory-judge",
            )

    valid = next(case for case in load_cases() if case.scenario == "advice" and case.locale == "vi")
    result = await run_reporting_suite((valid,), Judged())
    item = result.results[0]
    assert item.passed
    assert item.qualitative_scores is not None and item.qualitative_scores["groundedness"] == 0
    assert item.model_calls == 3
    assert result.hosted_quality == "ADVISORY_MEASURED"


@pytest.mark.asyncio
async def test_unrelated_fallback_cannot_pass_a_numeric_rejection_case():
    from work_management_ai.agents.reporting.contracts import ReportingContext
    from work_management_ai.evaluation.phase5_cases import ReportingEvalCase
    from work_management_ai.model_gateway.contracts import ModelGateway

    class WrongFailure(MockReportingEvaluationGateway):
        def for_case(
            self,
            case: ReportingEvalCase,
            context: ReportingContext,
            document: dict[str, Any],
            semantic: dict[str, Any],
        ) -> ModelGateway:
            if case.scenario == "unit":
                document = {"approved": True}
            return super().for_case(case, context, document, semantic)

    suite = await run_reporting_suite(load_cases(), WrongFailure())
    assert not suite.gate.passed
    assert any(not result.passed and result.id.endswith("unit") for result in suite.results)


@pytest.mark.asyncio
async def test_invalid_structured_output_retains_reported_token_usage():
    malformed = next(
        case for case in load_cases() if case.scenario == "malformed" and case.locale == "en"
    )
    suite = await run_reporting_suite((malformed,), MockReportingEvaluationGateway())
    item = suite.results[0]
    assert item.passed
    assert item.input_tokens == 10 and item.output_tokens == 10
    assert item.usage_missing_calls == 0


@pytest.mark.asyncio
async def test_failed_advisory_judge_is_unavailable_not_not_run():
    from work_management_ai.agents.reporting.contracts import ReportingContext
    from work_management_ai.evaluation.phase5_cases import ReportingEvalCase
    from work_management_ai.model_gateway.errors import ModelTimeoutError
    from work_management_ai.model_gateway.mock import MockModelGateway

    class UnavailableJudge:
        provider = "hosted"
        qualitative_judging = True

        def for_case(
            self,
            case: ReportingEvalCase,
            context: ReportingContext,
            document: dict[str, Any],
            semantic: dict[str, Any],
        ):
            return MockModelGateway(
                fixtures={
                    f"reporting.{case.locale}.draft": document,
                    f"reporting.{case.locale}.grounding": semantic,
                    "reporting.eval.rubric": ModelTimeoutError("private fixture error"),
                },
                model_ref="mock:judge",
            )

    valid = next(case for case in load_cases() if case.scenario == "advice" and case.locale == "en")
    suite = await run_reporting_suite((valid,), UnavailableJudge())
    assert suite.results[0].passed and suite.results[0].judge_status == "UNAVAILABLE"
    assert suite.hosted_quality == "UNAVAILABLE"
    assert "private fixture error" not in suite.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["number", "source", "malformed", "different_valid"])
async def test_gate_measures_actual_proposal_payload(mutation: str):
    from copy import deepcopy

    class MutatedProposal:
        async def run(self, harness: ReportingHarness, handoff: AgentHandoff) -> AgentResult:
            original = harness.tools.execute

            async def execute(request: ToolExecutionRequest):
                if request.tool_id == "reporting.propose":
                    body = deepcopy(request.typed_input)
                    narrative = cast(dict[str, Any], body["narrative"])
                    blocks = cast(list[dict[str, Any]], narrative["blocks"])
                    if mutation == "number":
                        blocks[0]["bindings"][0]["value"] = "9999"
                    elif mutation == "source":
                        for block in blocks:
                            if block.get("source_refs"):
                                block["source_refs"][0]["resource_id"] = str(uuid4())
                    elif mutation == "malformed":
                        body["narrative"] = {"approved": True}
                    else:
                        narrative["locale"] = "en" if narrative["locale"] == "vi" else "vi"
                    request = request.model_copy(update={"typed_input": body})
                return await original(request)

            harness.tools.execute = execute
            return await harness.run(handoff)

    suite = await run_reporting_suite(
        load_cases(), MockReportingEvaluationGateway(), execution=MutatedProposal()
    )
    assert not suite.gate.passed
    assert suite.failed > 0
    if mutation == "number":
        assert suite.gate.bindings_correct < suite.gate.bindings_checked
    if mutation == "source":
        assert suite.gate.cross_tenant_leakage_count > 0
