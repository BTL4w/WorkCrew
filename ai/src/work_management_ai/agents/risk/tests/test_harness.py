from uuid import uuid4

import pytest

from work_management_ai.agents.risk.harness import RiskHarness
from work_management_ai.model_gateway.mock import MockModelGateway
from work_management_ai.runtime.contracts import (
    ActorReference,
    AgentBudget,
    AgentHandoff,
    AgentId,
    AgentRunStatus,
    JsonValue,
    ResolvedActorContext,
    ToolExecutionRequest,
    ToolExecutionResult,
)


class Actors:
    async def resolve(self, reference: ActorReference) -> ResolvedActorContext:
        return ResolvedActorContext(**reference.model_dump(), role="MANAGER", is_active=True)


class Tools:
    def __init__(self):
        self.task = uuid4()
        self.assessment = uuid4()
        self.changed = False
        self.calls: list[ToolExecutionRequest] = []
        self.snapshot: dict[str, JsonValue] = dict(
            task_id=str(self.task),
            task_version=2,
            risk_assessment_id=str(self.assessment),
            version=1,
            fingerprint="a" * 64,
            state="READY",
            score="92",
            band="HIGH",
            scope="MANAGER",
            permitted_sources=[dict(id="task:1:v2", kind="TASK", values={"status": "IN_PROGRESS"})],
            observations=[dict(id="observation:0", text="Near deadline", source_ids=["task:1:v2"])],
            rationale="Stored rationale",
            limitations=[],
            recommendations=["Review blocker"],
            affected_week_ids=[],
        )

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        self.calls.append(request)
        if self.changed and len(self.calls) > 1:
            return ToolExecutionResult(status="REJECTED", typed_output={}, safe_error_code="STALE")
        return ToolExecutionResult(status="SUCCEEDED", typed_output=self.snapshot)


def handoff(tools: Tools, locale: str = "vi", **extra: JsonValue) -> AgentHandoff:
    return AgentHandoff(
        orchestration_run_id=uuid4(),
        parent_agent_run_id=uuid4(),
        target_agent_id=AgentId.RISK,
        target_agent_version="1.0.0",
        capability="risk.explain",
        objective="Explain risk",
        typed_input={
            "task_reference": str(tools.task),
            "locale": locale,
            "question": "Explain risk",
            **extra,
        },
        context_references=(),
        actor=ActorReference(organization_id=uuid4(), membership_id=uuid4()),
        budget=AgentBudget(
            max_iterations=4, max_tool_calls=4, max_model_attempts=1, timeout_seconds=90
        ),
        step_id="risk",
        idempotency_key="risk-test",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["vi", "en"])
async def test_risk_explanation_cannot_overwrite_persisted_ai_score(locale: str):
    tools = Tools()
    gateway = MockModelGateway(
        fixtures={
            f"risk.{locale}.explain": dict(
                observation_explanations=[
                    dict(
                        text="Deadline needs attention",
                        observation_ids=["observation:0"],
                        source_ids=["task:1:v2"],
                        assertions=[
                            dict(source_id="task:1:v2", field="status", value="IN_PROGRESS")
                        ],
                    )
                ],
                limitations=[],
                recommendations=["Review blocker"],
                replan_requested=True,
            )
        }
    )
    result = await RiskHarness(
        model_gateway=gateway, tool_executor=tools, actor_resolver=Actors()
    ).run(handoff(tools, locale))
    assert result.status is AgentRunStatus.COMPLETED
    assert result.typed_output["score"] == tools.snapshot["score"] == "92"
    assert result.requested_handoff is not None
    assert result.requested_handoff.target_capability == "planning.revise"
    assert {r.tool_id for r in tools.calls} == {"risk.read"}
    assert result.model_attempts_used == 1
    assert result.iterations_used <= 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        dict(score="0"),
        dict(
            observation_explanations=[
                dict(
                    text="Fabricated",
                    observation_ids=["observation:0"],
                    source_ids=["other-tenant"],
                )
            ],
            limitations=[],
            recommendations=[],
            replan_requested=False,
        ),
    ],
)
async def test_invalid_or_ungrounded_output_falls_back_to_stored_facts(
    output: dict[str, JsonValue],
):
    tools = Tools()
    result = await RiskHarness(
        model_gateway=MockModelGateway(fixtures={"risk.vi.explain": output}),
        tool_executor=tools,
        actor_resolver=Actors(),
    ).run(handoff(tools))
    assert result.status is AgentRunStatus.COMPLETED
    assert result.typed_output["fallback"] is True
    assert result.typed_output["rationale"] == "Stored rationale"
    assert result.typed_output["score"] == "92"


@pytest.mark.asyncio
async def test_revocation_or_stale_source_after_model_removes_sensitive_output():
    tools = Tools()
    tools.changed = True
    result = await RiskHarness(
        model_gateway=MockModelGateway(fixtures={}), tool_executor=tools, actor_resolver=Actors()
    ).run(handoff(tools))
    assert result.status is AgentRunStatus.FAILED
    assert "score" not in result.typed_output
    assert "rationale" not in result.typed_output


@pytest.mark.asyncio
async def test_model_cannot_grant_authority_or_tools():
    tools = Tools()
    result = await RiskHarness(
        model_gateway=MockModelGateway(fixtures={}), tool_executor=tools, actor_resolver=Actors()
    ).run(handoff(tools, role="MANAGER"))
    assert result.status is AgentRunStatus.FAILED
    assert not tools.calls


@pytest.mark.asyncio
async def test_timeout_uses_stored_rationale_with_one_attempt():
    from work_management_ai.model_gateway.errors import ModelTimeoutError

    tools = Tools()
    result = await RiskHarness(
        model_gateway=MockModelGateway(fixtures={"risk.vi.explain": ModelTimeoutError("timeout")}),
        tool_executor=tools,
        actor_resolver=Actors(),
    ).run(handoff(tools))
    assert result.status is AgentRunStatus.COMPLETED
    assert result.typed_output["fallback"] is True
    assert result.model_attempts_used == 1


@pytest.mark.asyncio
async def test_cross_tenant_handoff_context_is_rejected_before_model_or_tool():
    from datetime import UTC, datetime

    from work_management_ai.runtime.contracts import ContextReference

    tools = Tools()
    value = handoff(tools).model_copy(
        update={
            "context_references": (
                ContextReference(
                    reference_id=uuid4(),
                    organization_id=uuid4(),
                    resource_type="TASK",
                    resource_id=tools.task,
                    observed_at=datetime.now(UTC),
                ),
            )
        }
    )
    result = await RiskHarness(
        model_gateway=MockModelGateway(fixtures={}), tool_executor=tools, actor_resolver=Actors()
    ).run(value)
    assert result.status is AgentRunStatus.FAILED
    assert not tools.calls and result.model_attempts_used == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "The recorded risk score is 0 and the task is DONE.",
        "Điểm rủi ro là 0 và công việc đã hoàn thành.",
    ],
)
async def test_valid_citations_cannot_support_wrong_score_or_status(text: str):
    tools = Tools()
    output = {
        "observation_explanations": [
            {
                "text": text,
                "observation_ids": ["observation:0"],
                "source_ids": ["task:1:v2"],
                "assertions": [
                    {"source_id": "task:1:v2", "field": "status", "value": "IN_PROGRESS"}
                ],
            }
        ],
        "limitations": [],
        "recommendations": ["Review blocker"],
        "replan_requested": False,
    }
    result = await RiskHarness(
        model_gateway=MockModelGateway(fixtures={"risk.vi.explain": output}),
        tool_executor=tools,
        actor_resolver=Actors(),
    ).run(handoff(tools))
    assert result.typed_output["fallback"] is True
    assert result.typed_output["score"] == "92"


@pytest.mark.asyncio
@pytest.mark.parametrize("incorrect", [False, True])
async def test_assessment_score_and_source_status_assertions(incorrect: bool):
    tools = Tools()
    output = {
        "observation_explanations": [
            {
                "text": "The risk score is 92 and the task is IN_PROGRESS.",
                "observation_ids": ["observation:0"],
                "source_ids": ["task:1:v2"],
                "assertions": [
                    {
                        "source_id": "task:1:v2",
                        "field": "status",
                        "value": "DONE" if incorrect else "IN_PROGRESS",
                    },
                    {
                        "source_id": f"assessment:{tools.assessment}",
                        "field": "score",
                        "value": "92",
                    },
                ],
            }
        ],
        "limitations": [],
        "recommendations": [],
        "replan_requested": False,
    }
    result = await RiskHarness(
        model_gateway=MockModelGateway(fixtures={"risk.vi.explain": output}),
        tool_executor=tools,
        actor_resolver=Actors(),
    ).run(handoff(tools))
    assert result.status is AgentRunStatus.COMPLETED
    assert result.typed_output["fallback"] is incorrect
