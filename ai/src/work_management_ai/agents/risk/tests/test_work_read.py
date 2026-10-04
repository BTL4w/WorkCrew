"""Work Intelligence can read scoped Phase 4 facts and revalidate after synthesis."""

import pytest

from work_management_ai.agents.risk.tests.test_harness import Tools, handoff
from work_management_ai.agents.work_intelligence.harness import WorkIntelligenceHarness
from work_management_ai.model_gateway.mock import MockModelGateway
from work_management_ai.runtime.contracts import AgentBudget, AgentId, AgentRunStatus


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_work_read_progress_checks_current_sources_after_model(changed: bool):
    tools = Tools()
    tools.changed = changed
    value = handoff(tools).model_copy(
        update={
            "target_agent_id": AgentId.WORK_INTELLIGENCE,
            "capability": "work.answer_question",
            "typed_input": {
                "question": "Explain progress",
                "locale": "vi",
                "requested_kind": "PROGRESS",
            },
            "budget": AgentBudget(max_iterations=6, max_tool_calls=8, timeout_seconds=60),
        }
    )
    evidence_id = f"RISK_CONTEXT:{tools.task}:{'a' * 64}"
    fixtures = {
        "work_intelligence.vi.plan": dict(
            question_kind="PROGRESS",
            skill_reference="answer_work_question@1",
            tool_id="risk.read",
            tool_input={"task_reference": str(tools.task), "expected_fingerprint": None},
            requested_handoff=None,
        ),
        "work_intelligence.vi.synthesize": dict(
            question_kind="PROGRESS",
            claims=[
                dict(
                    text="Stored score is 92",
                    evidence_ids=[evidence_id],
                    assertions=[dict(evidence_id=evidence_id, field="score", value="92")],
                )
            ],
            needs_clarification=False,
            clarification_question=None,
        ),
    }
    result = await WorkIntelligenceHarness(
        model_gateway=MockModelGateway(fixtures=fixtures), tool_executor=tools
    ).run(value)
    assert result.status is (AgentRunStatus.FAILED if changed else AgentRunStatus.COMPLETED)
    assert len(tools.calls) == 2
    if changed:
        assert result.typed_output["evidence"] == []
    else:
        evidence = result.typed_output["evidence"]
        assert isinstance(evidence, list)
        first = evidence[0]
        assert isinstance(first, dict)
        assert first["resource_type"] == "RISK_CONTEXT"
