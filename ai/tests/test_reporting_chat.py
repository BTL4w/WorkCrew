# pyright: reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportIndexIssue=false
"""Chat contracts and capability mode cannot manufacture approval or publication."""

import pytest
from pydantic import ValidationError

from work_management_ai.agents.orchestrator.contracts import ReportIntent


def test_report_intent_rejects_authority_and_external_links():
    with pytest.raises(ValidationError):
        ReportIntent.model_validate(
            dict(
                operation="PREPARE_REPORT",
                project_reference="Conference",
                locale="vi",
                approval=True,
            )
        )
    value = ReportIntent(
        operation="EXPLAIN_STATUS", project_reference="Conference 2026", locale="en"
    )
    assert value.operation == "EXPLAIN_STATUS"
    assert value.kind == "DAILY"


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["vi", "en"])
@pytest.mark.parametrize("invalid", [False, True])
@pytest.mark.parametrize("exhausted", [False, True])
async def test_status_returns_verified_or_metrics_only_without_proposal(locale, invalid, exhausted):
    from typing import cast
    from uuid import NAMESPACE_URL, uuid5

    from work_management_ai.agents.reporting.harness import ReportingHarness
    from work_management_ai.agents.reporting.tests.test_harness import Actors, setup
    from work_management_ai.model_gateway.mock import MockModelGateway
    from work_management_ai.runtime.contracts import JsonValue, ToolExecutionResult

    wire, handoff, _actors, tools = setup(locale)
    context = tools.context
    from work_management_ai.agents.reporting.tests.fixtures import narrative_wire
    from work_management_ai.agents.reporting.tests.test_harness import verdict

    doc = narrative_wire(wire, locale)
    gateway = MockModelGateway(
        fixtures={f"reporting.{locale}.draft": doc, f"reporting.{locale}.grounding": verdict(doc)}
    )
    handoff = handoff.model_copy(
        update={
            "capability": "reporting.explain_snapshot",
            "typed_input": {
                "operation": "EXPLAIN_STATUS",
                "project_reference": "Conference 2026",
                "locale": locale,
            },
        }
    )
    wire = context["snapshot"]
    card = dict(
        kind="project_status",
        project_id=wire["project_id"],
        project_label="Conference 2026",
        context_run_id=str(uuid5(NAMESPACE_URL, f"agent-run:{handoff.idempotency_key}")),
        snapshot_id=wire["id"],
        snapshot_hash=wire["snapshot_hash"],
        period_start=wire["period"]["local_start"],
        period_end=wire["period"]["local_end"],
        timezone="UTC",
        report_kind="DAILY",
        captured_at=wire["captured_at"],
        metrics=[],
        sources=[],
        limitations=[],
        analysis=[],
        analysis_state="UNAVAILABLE",
    )

    class ChatTools:
        async def execute(self, request):
            if request.tool_id != "reporting.chat":
                raise AssertionError("status cannot propose or publish")
            return ToolExecutionResult(
                status="SUCCEEDED",
                typed_output=cast(
                    dict[str, JsonValue], dict(resolution="UNIQUE", card=card, context=context)
                ),
            )

    if invalid:
        gateway = MockModelGateway(fixtures={})
    from work_management_ai.agents.reporting.contracts import ReportingUsage

    class Usage:
        async def load(self):
            return ReportingUsage(
                attempts=3 if exhausted else 0,
                tools=0,
                input_reserved=0,
                output_reserved=0,
                retries=1 if exhausted else 0,
            )

        async def remaining_seconds(self):
            return 180.0

        async def reserve(self, *, input_tokens, output_tokens):
            assert not exhausted, "restart must not reset exhausted budget"
            return 1

        async def reserve_tool(self, invocation_key):
            pass

        async def consume_retry(self):
            pass

    result = await ReportingHarness(
        model_gateway=gateway,
        tool_executor=ChatTools(),
        actor_resolver=Actors(handoff.actor),
        usage=Usage(),
    ).run(handoff)
    assert result.status == "COMPLETED"
    returned = result.typed_output["card"]
    assert isinstance(returned, dict)
    assert returned["analysis_state"] == ("UNAVAILABLE" if invalid or exhausted else "VERIFIED")
    if exhausted:
        assert result.model_attempts_used == 3
    assert not result.proposed_actions
