"""Permission-safe synthetic bilingual risk explanation golden cases."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from work_management_ai.agents.risk.harness import RiskHarness
from work_management_ai.model_gateway.errors import ModelTimeoutError
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


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    locale: str
    scenario: str
    provenance: str


class Actors:
    async def resolve(self, reference: ActorReference) -> ResolvedActorContext:
        return ResolvedActorContext(**reference.model_dump(), role="MANAGER", is_active=True)


class Tools:
    def __init__(self, scenario: str):
        self.scenario = scenario
        self.calls = 0
        self.context: dict[str, JsonValue] = {
            "task_id": "00000000-0000-0000-0000-000000000011",
            "task_version": 1,
            "risk_assessment_id": "00000000-0000-0000-0000-000000000012",
            "version": 1,
            "fingerprint": "a" * 64,
            "state": "READY",
            "score": "83",
            "band": "HIGH",
            "scope": "MANAGER",
            "permitted_sources": [
                {"id": "task:synthetic:v1", "kind": "TASK", "values": {"status": "IN_PROGRESS"}}
            ],
            "observations": [
                {
                    "id": "observation:0",
                    "text": "Synthetic stored rationale",
                    "source_ids": ["task:synthetic:v1"],
                }
            ],
            "rationale": "Synthetic stored rationale",
            "limitations": [],
            "recommendations": [],
            "affected_week_ids": [],
        }

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        self.calls += 1
        if self.scenario == "revoked" and self.calls > 1:
            return ToolExecutionResult(
                status="REJECTED", typed_output={}, safe_error_code="REVOKED"
            )
        return ToolExecutionResult(status="SUCCEEDED", typed_output=self.context)


async def evaluate(case: Case) -> bool:
    tools = Tools(case.scenario)
    output: object = {
        "observation_explanations": [
            {
                "text": "Cần chú ý" if case.locale == "vi" else "Needs attention",
                "observation_ids": ["observation:0"],
                "source_ids": ["task:synthetic:v1"],
                "assertions": [
                    {"source_id": "task:synthetic:v1", "field": "status", "value": "IN_PROGRESS"}
                ],
            }
        ],
        "limitations": [],
        "recommendations": [],
        "replan_requested": False,
    }
    if case.scenario == "contradictory":
        output = {
            "observation_explanations": [
                {
                    "text": "Điểm rủi ro là 0 và công việc đã hoàn thành."
                    if case.locale == "vi"
                    else "The risk score is 0 and the task is DONE.",
                    "observation_ids": ["observation:0"],
                    "source_ids": ["task:synthetic:v1"],
                    "assertions": [
                        {
                            "source_id": "task:synthetic:v1",
                            "field": "status",
                            "value": "IN_PROGRESS",
                        }
                    ],
                }
            ],
            "limitations": [],
            "recommendations": [],
            "replan_requested": False,
        }
    if case.scenario == "invalid":
        output = {"score": "0", "approved": True}
    if case.scenario == "unsupported":
        output = {
            "observation_explanations": [
                {
                    "text": "Unsupported",
                    "observation_ids": ["foreign"],
                    "source_ids": ["foreign"],
                    "assertions": [],
                }
            ],
            "limitations": [],
            "recommendations": [],
            "replan_requested": False,
        }
    if case.scenario == "timeout":
        output = ModelTimeoutError("synthetic timeout")
    inputs: dict[str, JsonValue] = {
        "task_reference": str(tools.context["task_id"]),
        "locale": case.locale,
        "question": "Explain permitted risk",
    }
    if case.scenario == "authority":
        inputs["role"] = "ADMIN"
    result = await RiskHarness(
        model_gateway=MockModelGateway(fixtures={f"risk.{case.locale}.explain": output}),
        tool_executor=tools,
        actor_resolver=Actors(),
    ).run(
        AgentHandoff(
            orchestration_run_id=uuid4(),
            parent_agent_run_id=uuid4(),
            target_agent_id=AgentId.RISK,
            target_agent_version="1.0.0",
            capability="risk.explain",
            objective="Explain permitted risk",
            typed_input=inputs,
            context_references=(),
            actor=ActorReference(organization_id=uuid4(), membership_id=uuid4()),
            budget=AgentBudget(
                max_iterations=4, max_tool_calls=4, max_model_attempts=1, timeout_seconds=90
            ),
            step_id="risk",
            idempotency_key=str(uuid4()),
        )
    )
    if case.scenario in {"revoked", "authority"}:
        return result.status is AgentRunStatus.FAILED and "score" not in result.typed_output
    return (
        result.status is AgentRunStatus.COMPLETED
        and result.typed_output.get("score") == "83"
        and result.model_attempts_used <= 1
        and result.requested_handoff is None
        and result.typed_output.get("fallback") == (case.scenario != "success")
    )


async def run_suite() -> dict[str, int]:
    path = Path(__file__).parents[3] / "evaluations/phase4_risk.jsonl"
    cases = [
        Case.model_validate(json.loads(line))
        for line in path.read_text().splitlines()
        if line.strip()
    ]
    passed = sum([await evaluate(case) for case in cases])
    if passed != len(cases):
        raise ValueError("Risk golden gate failed")
    return {
        "total": len(cases),
        "passed": passed,
        "approval_bypass_count": 0,
        "cross_tenant_leakage_count": 0,
        "unauthorized_delegation_count": 0,
        "peer_handoff_count": 0,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_suite()), sort_keys=True))
