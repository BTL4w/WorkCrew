"""One model attempt between authorized read and exact-context revalidation."""

import asyncio
import json
from importlib.resources import files
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from work_management_ai.agents.orchestrator.contracts import ActorContextResolverPort
from work_management_ai.agents.risk.contracts import (
    ObservationExplanation,
    RiskExplanation,
    RiskExplanationInput,
    RiskQuestion,
    RiskReplanRequest,
)
from work_management_ai.agents.risk.evaluators.grounding import verify_explanation
from work_management_ai.agents.risk.prompts.system_v1 import SYSTEM_V1
from work_management_ai.agents.risk.workflows.graph import RiskGraph, RiskState
from work_management_ai.model_gateway.contracts import (
    ModelGateway,
    ModelMessage,
    StructuredModelRequest,
)
from work_management_ai.runtime.contracts import (
    AgentHandoff,
    AgentId,
    AgentResult,
    AgentRunStatus,
    JsonValue,
    RequestedHandoff,
    ToolExecutionRequest,
    ToolExecutorPort,
    VerifierResult,
)
from work_management_ai.runtime.manifests import AgentManifest, SkillManifest, load_yaml_resource
from work_management_ai.runtime.policy_guard import PolicyGuard


class RiskHarness:
    def __init__(
        self,
        *,
        model_gateway: ModelGateway,
        tool_executor: ToolExecutorPort,
        actor_resolver: ActorContextResolverPort,
    ):
        self.gateway = model_gateway
        self.tools = tool_executor
        self.actors = actor_resolver
        self.manifest = load_yaml_resource(
            "work_management_ai.agents.risk", "agent.yaml", AgentManifest
        )

    async def run(self, handoff: AgentHandoff) -> AgentResult:
        runner = _RiskRun(self, handoff)
        state = RiskState()
        try:
            async with asyncio.timeout(min(90, handoff.budget.timeout_seconds)):
                await RiskGraph().run(runner, state, handoff.budget.max_iterations)
            context, explanation = state.context, state.explanation
            if context is None or explanation is None:
                raise ValueError("RISK_CONTEXT_REQUIRED")
            replan = RiskReplanRequest(
                fingerprint=context.fingerprint,
                affected_week_ids=context.affected_week_ids,
                observation_ids=tuple(o.id for o in context.observations),
                risk_assessment_id=context.risk_assessment_id,
                task_id=context.task_id,
            )
            requested = (
                RequestedHandoff(
                    target_capability="planning.revise",
                    objective=runner.value.question,
                    typed_input=cast(dict[str, JsonValue], replan.model_dump(mode="json")),
                )
                if explanation.replan_requested and context.scope == "MANAGER"
                else None
            )
            if context.scope != "MANAGER" and explanation.replan_requested:
                explanation = explanation.model_copy(update={"replan_requested": False})
            output = {
                **context.model_dump(mode="json"),
                "explanation": explanation.model_dump(mode="json"),
                "fallback": state.fallback,
            }
            return AgentResult(
                agent_id=AgentId.RISK,
                agent_version="1.0.0",
                status=AgentRunStatus.COMPLETED,
                typed_output=cast(dict[str, JsonValue], output),
                requested_handoff=requested,
                iterations_used=state.iterations,
                tool_calls_used=state.tools,
                model_attempts_used=state.attempts,
                stop_reason="STORED_RATIONALE_FALLBACK" if state.fallback else "COMPLETED",
                verifier_results=(
                    VerifierResult(
                        verifier_id="risk_grounding", verifier_version="1.0.0", passed=True
                    ),
                ),
            )
        except Exception:
            return AgentResult(
                agent_id=AgentId.RISK,
                agent_version="1.0.0",
                status=AgentRunStatus.FAILED,
                typed_output={"fallback": "manual_risk"},
                iterations_used=state.iterations,
                tool_calls_used=state.tools,
                model_attempts_used=state.attempts,
                stop_reason="RISK_CONTEXT_UNAVAILABLE",
                safe_error_code="RISK_CONTEXT_UNAVAILABLE",
            )


class _RiskRun:
    def __init__(self, harness: RiskHarness, handoff: AgentHandoff):
        self.harness, self.handoff = harness, handoff
        self.value = RiskQuestion(task_reference="unresolved", locale="en", question="unresolved")
        self.instructions = ""

    async def read(self, state: RiskState, expected: str | None = None) -> RiskExplanationInput:
        if state.tools >= self.handoff.budget.max_tool_calls:
            raise ValueError("RISK_TOOL_BUDGET")
        state.tools += 1
        result = await self.harness.tools.execute(
            ToolExecutionRequest(
                agent_run_id=uuid5(NAMESPACE_URL, f"agent-run:{self.handoff.idempotency_key}"),
                tool_id="risk.read",
                tool_version="1.0.0",
                call_id=f"{self.handoff.step_id}:read:{state.tools}",
                actor=self.handoff.actor,
                typed_input={
                    "task_reference": self.value.task_reference,
                    "expected_fingerprint": expected,
                },
                idempotency_key=f"{self.handoff.idempotency_key}:read:{state.tools}",
            )
        )
        if result.status != "SUCCEEDED":
            raise ValueError("RISK_READ_DENIED")
        return RiskExplanationInput.model_validate(result.typed_output)

    async def execute_node(self, node: str, state: RiskState) -> None:
        if node == "authorize":
            actor = await self.harness.actors.resolve(self.handoff.actor)
            PolicyGuard().authorize_handoff(
                current_actor=actor,
                parent_agent_id=AgentId.ORCHESTRATOR,
                handoff=self.handoff,
                manifest=self.harness.manifest,
                requested_tool_ids=("risk.read@1",),
                requested_skill_ids=self.harness.manifest.allowed_skills,
            )
            if any(
                r.organization_id != actor.organization_id for r in self.handoff.context_references
            ):
                raise ValueError("RISK_CONTEXT_TENANT")
            self.value = RiskQuestion.model_validate(self.handoff.typed_input)
            instructions: list[str] = []
            for ref in self.harness.manifest.allowed_skills:
                package = f"work_management_ai.skills.{ref.split('@')[0]}"
                load_yaml_resource(package, "skill.yaml", SkillManifest)
                instructions.append(files(package).joinpath("SKILL.md").read_text())
            self.instructions = "\n".join(instructions)
        elif node == "read":
            state.context = await self.read(state)
        elif node == "explain":
            context = state.context
            if context is None:
                raise ValueError("RISK_CONTEXT_REQUIRED")
            try:
                if self.handoff.budget.max_model_attempts < 1:
                    raise ValueError("RISK_MODEL_BUDGET")
                content = json.dumps(
                    {
                        "question": self.value.question,
                        "locale": self.value.locale,
                        "context": context.model_dump(mode="json"),
                    },
                    ensure_ascii=False,
                )
                # UTF-8 bytes bound conservatively bounds token count for text.
                input_bound = (
                    len(content.encode())
                    + len((SYSTEM_V1 + self.instructions).encode())
                    + len(json.dumps(RiskExplanation.model_json_schema()).encode())
                    + 1024
                )
                if input_bound > self.handoff.budget.max_input_tokens:
                    raise ValueError("RISK_INPUT_BUDGET")
                state.attempts += 1
                response = await self.harness.gateway.generate_structured(
                    StructuredModelRequest(
                        invocation_key=f"risk.{self.value.locale}.explain",
                        messages=(
                            ModelMessage(
                                role="system", content=SYSTEM_V1 + "\n" + self.instructions
                            ),
                            ModelMessage(role="user", content=content),
                        ),
                        output_schema=RiskExplanation,
                        timeout_seconds=min(60, self.handoff.budget.timeout_seconds),
                        max_output_tokens=self.handoff.budget.max_output_tokens,
                    )
                )
                verify_explanation(context, response.parsed)
                state.explanation = response.parsed
            except Exception:
                state.fallback = True
                state.explanation = RiskExplanation(
                    observation_explanations=tuple(
                        ObservationExplanation(
                            text=o.text,
                            observation_ids=(o.id,),
                            source_ids=o.source_ids,
                            assertions=(),
                        )
                        for o in context.observations[:10]
                        if not o.id.startswith("fact:")
                    ),
                    limitations=context.limitations[:10],
                    recommendations=context.recommendations[:5],
                    replan_requested=False,
                )
        elif node == "revalidate":
            if state.context is None:
                raise ValueError("RISK_CONTEXT_REQUIRED")
            current = await self.read(state, state.context.fingerprint)
            if current != state.context:
                raise ValueError("RISK_CONTEXT_CHANGED")
