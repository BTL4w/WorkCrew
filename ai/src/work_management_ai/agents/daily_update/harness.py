"""Guarded specialist: typed report interpretation ends at owner review."""

import asyncio
import json
from importlib.resources import files
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from work_management_ai.agents.daily_update.contracts import (
    DailyUpdateHandoff,
    DailyUpdateResult,
    ExtractedReport,
    ReportingSnapshot,
)
from work_management_ai.agents.daily_update.evaluators.grounding import verify_context
from work_management_ai.agents.daily_update.prompts.system_v1 import SYSTEM_V1
from work_management_ai.agents.daily_update.workflows.graph import (
    DailyUpdateGraph,
    DailyUpdateState,
)
from work_management_ai.agents.orchestrator.contracts import ActorContextResolverPort
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
    ToolExecutionRequest,
    ToolExecutorPort,
    VerifierResult,
)
from work_management_ai.runtime.manifests import AgentManifest, SkillManifest, load_yaml_resource
from work_management_ai.runtime.policy_guard import PolicyGuard
from work_management_ai.tools.daily_update.contracts import DailyUpdateToolInput


class DailyUpdateHarness:
    def __init__(
        self,
        *,
        model_gateway: ModelGateway,
        tool_executor: ToolExecutorPort,
        actor_resolver: ActorContextResolverPort,
    ) -> None:
        self.gateway = model_gateway
        self.tools = tool_executor
        self.actors = actor_resolver
        self.manifest = load_yaml_resource(
            "work_management_ai.agents.daily_update", "agent.yaml", AgentManifest
        )
        self.graph = DailyUpdateGraph(self)

    async def run(self, handoff: AgentHandoff) -> AgentResult:
        state = DailyUpdateState(
            handoff=handoff,
            value=None,
            tool_input=None,
            report=None,
            card=None,
            instructions="",
            iterations=0,
            model_attempts=0,
            tool_calls=0,
            status=AgentRunStatus.RUNNING,
            stopped=False,
            result=None,
        )
        try:
            async with asyncio.timeout(handoff.budget.timeout_seconds):
                state = await self.graph.run(state)
        except Exception:
            state["status"] = AgentRunStatus.FAILED
        status = state["status"]
        output: dict[str, JsonValue]
        if status is AgentRunStatus.AWAITING_HUMAN and state["card"] is not None:
            output = cast(dict[str, JsonValue], state["card"].model_dump(mode="json"))
            code = "AWAIT_OWNER"
        elif status is AgentRunStatus.AWAITING_INPUT:
            output = {"question_key": "dailyUpdate.chat.clarify"}
            code = "NEEDS_INPUT"
        else:
            status = AgentRunStatus.FAILED
            output = {"fallback": "manual_daily_update"}
            code = "DAILY_UPDATE_MANUAL_FALLBACK"
        return AgentResult(
            agent_id=AgentId.DAILY_UPDATE,
            agent_version="1.0.0",
            status=status,
            typed_output=output,
            requested_handoff=None,
            model_attempts_used=state["model_attempts"],
            iterations_used=state["iterations"],
            tool_calls_used=state["tool_calls"],
            stop_reason=code,
            safe_error_code=code if status is AgentRunStatus.FAILED else None,
            verifier_results=(
                VerifierResult(
                    verifier_id="daily_update_grounding",
                    verifier_version="1.0.0",
                    passed=status is not AgentRunStatus.FAILED,
                ),
            ),
        )

    async def _tool(
        self, state: DailyUpdateState, value: DailyUpdateToolInput
    ) -> dict[str, JsonValue]:
        handoff = state["handoff"]
        if state["tool_calls"] >= handoff.budget.max_tool_calls:
            raise ValueError("DAILY_UPDATE_TOOL_BUDGET")
        state["tool_calls"] += 1
        result = await self.tools.execute(
            ToolExecutionRequest(
                agent_run_id=uuid5(NAMESPACE_URL, f"agent-run:{handoff.idempotency_key}"),
                tool_id="daily_update.prepare",
                tool_version="1.0.0",
                call_id=f"{handoff.step_id}:{value.action}",
                actor=handoff.actor,
                typed_input=cast(dict[str, JsonValue], value.model_dump(mode="json")),
                idempotency_key=str(
                    uuid5(NAMESPACE_URL, f"daily:{handoff.idempotency_key}:{value.action}")
                ),
            )
        )
        if result.status != "SUCCEEDED":
            raise ValueError(result.safe_error_code or "DAILY_UPDATE_TOOL_FAILED")
        return result.typed_output

    async def execute_node(self, node: str, state: DailyUpdateState) -> None:
        handoff = state["handoff"]
        if node == "authorize":
            actor = await self.actors.resolve(handoff.actor)
            PolicyGuard().authorize_handoff(
                current_actor=actor,
                parent_agent_id=AgentId.ORCHESTRATOR,
                handoff=handoff,
                manifest=self.manifest,
                requested_tool_ids=("daily_update.prepare@1",),
                requested_skill_ids=self.manifest.allowed_skills,
            )
            if any(r.organization_id != actor.organization_id for r in handoff.context_references):
                raise ValueError("DAILY_UPDATE_CONTEXT_TENANT_MISMATCH")
            if handoff.budget.max_model_attempts < 1:
                raise ValueError("DAILY_UPDATE_MODEL_BUDGET")
            value = DailyUpdateHandoff.model_validate(handoff.typed_input)
            state["value"] = value
            instructions: list[str] = []
            for reference in self.manifest.allowed_skills:
                name = reference.split("@", maxsplit=1)[0]
                package = f"work_management_ai.skills.{name}"
                load_yaml_resource(package, "skill.yaml", SkillManifest)
                instructions.append(files(package).joinpath("SKILL.md").read_text())
            state["instructions"] = "\n".join(instructions)
            state["tool_input"] = DailyUpdateToolInput(
                action="CONTEXT",
                task_id=value.task_id,
                task_version=value.task_version,
                evidence_refs=value.evidence_refs,
                draft_id=value.draft_id,
                draft_version=value.draft_version,
            )
            return
        value, base = state["value"], state["tool_input"]
        if value is None or base is None:
            raise ValueError("DAILY_UPDATE_STATE_INVALID")
        if node == "context":
            snapshot = ReportingSnapshot.model_validate(await self._tool(state, base))
            verify_context(value, snapshot)
        elif node == "extract_report":
            state["model_attempts"] += 1
            response = await self.gateway.generate_structured(
                StructuredModelRequest(
                    invocation_key=f"daily_update.{value.locale}.extract",
                    messages=(
                        ModelMessage(
                            role="system", content=SYSTEM_V1 + "\n" + state["instructions"]
                        ),
                        ModelMessage(
                            role="user",
                            content=json.dumps(
                                {"report": value.text, "locale": value.locale}, ensure_ascii=False
                            ),
                        ),
                    ),
                    output_schema=ExtractedReport,
                    timeout_seconds=min(60, handoff.budget.timeout_seconds),
                    max_output_tokens=min(1500, handoff.budget.max_output_tokens),
                )
            )
            report = response.parsed
            state["report"] = report
            if (
                report.needs_clarification
                or report.reported_percent is None
                or not report.done_text.strip()
            ):
                state["status"] = AgentRunStatus.AWAITING_INPUT
                state["stopped"] = True
        elif node == "save_draft":
            report = state["report"]
            if report is None:
                raise ValueError("DAILY_UPDATE_REPORT_REQUIRED")
            state["card"] = DailyUpdateResult.model_validate(
                await self._tool(
                    state, base.model_copy(update={"action": "DRAFT", "report": report})
                )
            )
        elif node == "assess_originals":
            card = state["card"]
            if card is None:
                raise ValueError("DAILY_UPDATE_DRAFT_REQUIRED")
            state["card"] = DailyUpdateResult.model_validate(
                await self._tool(
                    state,
                    base.model_copy(
                        update={
                            "action": "ASSESS",
                            "draft_id": card.draft_id,
                            "draft_version": card.draft_version,
                        }
                    ),
                )
            )
        elif node == "await_owner":
            state["status"] = AgentRunStatus.AWAITING_HUMAN
            state["stopped"] = True
        else:
            raise ValueError("DAILY_UPDATE_NODE_NOT_ALLOWED")
