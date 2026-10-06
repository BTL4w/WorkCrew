"""Bounded specialist: immutable facts, grounded draft, current access, human gate."""

import asyncio
import json
from importlib.resources import files
from time import monotonic
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel

from work_management_ai.agents.orchestrator.contracts import ActorContextResolverPort
from work_management_ai.agents.reporting.context import SOURCE_DETAIL_LIMIT, model_context
from work_management_ai.agents.reporting.contracts import (
    ReportingContext,
    ReportingNarrative,
    ReportingProposal,
    ReportingRequest,
    SemanticVerdict,
)
from work_management_ai.agents.reporting.evaluators.grounding import verify_grounding
from work_management_ai.agents.reporting.evaluators.numeric import verify_numeric
from work_management_ai.agents.reporting.prompts.grounding_v1 import GROUNDING_V1
from work_management_ai.agents.reporting.prompts.summary_v1 import SUMMARY_V1
from work_management_ai.agents.reporting.prompts.system_v1 import SYSTEM_V1
from work_management_ai.agents.reporting.usage import ReportingUsagePort, reporting_attempt_scope
from work_management_ai.agents.reporting.workflows.graph import ReportingGraph, ReportingState
from work_management_ai.model_gateway.contracts import (
    ModelGateway,
    ModelMessage,
    StructuredModelRequest,
)
from work_management_ai.model_gateway.errors import ModelRateLimitError, ModelTimeoutError
from work_management_ai.runtime.contracts import (
    AgentHandoff,
    AgentId,
    AgentResult,
    AgentRunStatus,
    JsonValue,
    ProposedAction,
    RiskLevel,
    ToolExecutionRequest,
    ToolExecutorPort,
)
from work_management_ai.runtime.manifests import (
    AgentManifest,
    SkillManifest,
    canonical_manifest_fingerprint,
    load_yaml_resource,
)
from work_management_ai.runtime.policy_guard import PolicyGuard
from work_management_ai.tools.reporting.contracts import ProposalReceipt


class ReportingHarness:
    def __init__(
        self,
        *,
        model_gateway: ModelGateway,
        tool_executor: ToolExecutorPort,
        actor_resolver: ActorContextResolverPort,
        usage: ReportingUsagePort | None = None,
    ):
        self.usage = usage
        self.gateway, self.tools, self.actors = model_gateway, tool_executor, actor_resolver
        self.manifest = load_yaml_resource(
            "work_management_ai.agents.reporting", "agent.yaml", AgentManifest
        )

    async def run(self, handoff: AgentHandoff) -> AgentResult:
        if handoff.capability in {"reporting.prepare_report", "reporting.explain_snapshot"}:
            return await self.run_chat(handoff)
        state = ReportingState()
        runner = _ReportingRun(self, handoff)
        try:
            if self.usage is not None:
                usage = await self.usage.load()
                state.attempts, state.tools = usage.attempts, usage.tools
                state.input_reserved, state.output_reserved = (
                    usage.input_reserved,
                    usage.output_reserved,
                )
                state.retry_used = usage.retries > 0
                runner.deadline = monotonic() + min(
                    180, handoff.budget.timeout_seconds, await self.usage.remaining_seconds()
                )
            async with asyncio.timeout(max(0, runner.deadline - monotonic())):
                await ReportingGraph().run(runner, state, handoff.budget.max_iterations)
            if state.narrative is None or state.proposal_id is None:
                raise ValueError("REPORTING_PROPOSAL_REQUIRED")
            return AgentResult(
                agent_id=AgentId.REPORTING,
                agent_version="1.0.0",
                status=AgentRunStatus.AWAITING_HUMAN,
                typed_output=cast(
                    dict[str, JsonValue],
                    {
                        "narrative": state.narrative.model_dump(mode="json"),
                        "version_id": state.proposal_id,
                    },
                ),
                proposed_actions=(
                    ProposedAction(
                        action_type="reporting.review",
                        risk=RiskLevel.PROPOSAL_ONLY,
                        requires_human_gate=True,
                    ),
                ),
                verifier_results=tuple(state.verifiers),
                model_attempts_used=state.attempts,
                iterations_used=state.iterations,
                tool_calls_used=state.tools,
                stop_reason="AWAITING_MANAGER_REVIEW",
            )
        except Exception:
            return AgentResult(
                agent_id=AgentId.REPORTING,
                agent_version="1.0.0",
                status=AgentRunStatus.FAILED,
                typed_output={"fallback": "metrics_only"},
                verifier_results=tuple(state.verifiers),
                model_attempts_used=state.attempts,
                iterations_used=state.iterations,
                tool_calls_used=state.tools,
                stop_reason="REPORTING_UNAVAILABLE",
                safe_error_code="REPORTING_UNAVAILABLE",
            )

    async def run_chat(self, handoff: AgentHandoff) -> AgentResult:
        from work_management_ai.agents.orchestrator.contracts import ReportIntent
        from work_management_ai.runtime.contracts import (
            ProjectStatusResponseBlock,
            ReportResponseBlock,
        )

        state = ReportingState()
        runner = _ReportingRun(self, handoff)
        try:
            if self.usage is not None:
                usage = await self.usage.load()
                state.attempts, state.tools = usage.attempts, usage.tools
                state.input_reserved, state.output_reserved = (
                    usage.input_reserved,
                    usage.output_reserved,
                )
                state.retry_used = usage.retries > 0
                runner.deadline = monotonic() + min(
                    180, handoff.budget.timeout_seconds, await self.usage.remaining_seconds()
                )
            await runner.authorize()
            intent = ReportIntent.model_validate(handoff.typed_input)
            capability = (
                "reporting.prepare_report"
                if intent.operation == "PREPARE_REPORT"
                else "reporting.explain_snapshot"
            )
            if capability != handoff.capability:
                raise ValueError("REPORT_CHAT_MODE")
            async with asyncio.timeout(max(0, runner.deadline - monotonic())):
                raw = await runner.tool(
                    state,
                    "reporting.chat",
                    cast(dict[str, JsonValue], intent.model_dump(mode="json")),
                )
                if raw.get("resolution") != "UNIQUE":
                    return AgentResult(
                        agent_id=AgentId.REPORTING,
                        agent_version="1.0.0",
                        status=AgentRunStatus.AWAITING_INPUT,
                        typed_output={
                            "question": "Vui lòng chọn hoặc chỉ rõ dự án bằng tên hoặc mã."
                            if intent.locale == "vi"
                            else "Please select or specify the project by name or ID.",
                            "response_context": {
                                "reporting": True,
                                "candidates": raw.get("candidates", []),
                            },
                        },
                        iterations_used=1,
                        tool_calls_used=state.tools,
                        stop_reason="PROJECT_CLARIFICATION_REQUIRED",
                    )
                if intent.operation == "PREPARE_REPORT":
                    card = ReportResponseBlock.model_validate(raw["card"])
                    return AgentResult(
                        agent_id=AgentId.REPORTING,
                        agent_version="1.0.0",
                        status=AgentRunStatus.AWAITING_HUMAN,
                        typed_output={"card": cast(JsonValue, card.model_dump(mode="json"))},
                        iterations_used=1,
                        tool_calls_used=state.tools,
                        stop_reason="AWAITING_MANAGER_REVIEW",
                    )
                context = ReportingContext.model_validate(raw["context"])
                card = ProjectStatusResponseBlock.model_validate(raw["card"])
                if (
                    context.snapshot.organization_id != handoff.actor.organization_id
                    or not context.snapshot.verified_hash()
                    or context.snapshot.snapshot_hash != card.snapshot_hash
                ):
                    raise ValueError("STATUS_CONTEXT_MISMATCH")
                try:
                    narrative = await runner.model(
                        state,
                        key=f"reporting.{intent.locale}.draft",
                        system=SYSTEM_V1,
                        payload=model_context(context),
                        schema=ReportingNarrative,
                    )
                    numeric = verify_numeric(
                        context.snapshot, narrative, source_detail_limit=SOURCE_DETAIL_LIMIT
                    )
                    state.verifiers.append(numeric)
                    if not numeric.passed:
                        raise ValueError("STATUS_NUMERIC_REJECTED")
                    semantic = await runner.model(
                        state,
                        key=f"reporting.{intent.locale}.grounding",
                        system=GROUNDING_V1,
                        payload={
                            "context": model_context(context),
                            "narrative": narrative.model_dump(mode="json"),
                        },
                        schema=SemanticVerdict,
                    )
                    grounded = verify_grounding(narrative, semantic)
                    state.verifiers.append(grounded)
                    if not grounded.passed or narrative.locale != intent.locale:
                        raise ValueError("STATUS_SEMANTIC_REJECTED")
                    from work_management_ai.agents.reporting.contracts import NarrativeTextBlock

                    analysis = tuple(
                        b.text for b in narrative.blocks if isinstance(b, NarrativeTextBlock)
                    )[:3]
                    card = card.model_copy(
                        update={"analysis": analysis, "analysis_state": "VERIFIED"}
                    )
                except Exception:
                    # Only verified snapshot facts survive model failure.
                    card = card.model_copy(update={"analysis": (), "analysis_state": "UNAVAILABLE"})
                current = await runner.tool(
                    state,
                    "reporting.chat",
                    {"operation": "REAUTHORIZE", "context_run_id": str(card.context_run_id)},
                )
                if current.get("context") != raw.get("context"):
                    raise ValueError("STATUS_ACCESS_CHANGED")
                await runner.authorize()
                return AgentResult(
                    agent_id=AgentId.REPORTING,
                    agent_version="1.0.0",
                    status=AgentRunStatus.COMPLETED,
                    typed_output={"card": cast(JsonValue, card.model_dump(mode="json"))},
                    verifier_results=tuple(state.verifiers),
                    model_attempts_used=state.attempts,
                    iterations_used=7,
                    tool_calls_used=state.tools,
                    stop_reason="STATUS_VERIFIED",
                )
        except Exception:
            return AgentResult(
                agent_id=AgentId.REPORTING,
                agent_version="1.0.0",
                status=AgentRunStatus.FAILED,
                typed_output={},
                model_attempts_used=state.attempts,
                tool_calls_used=state.tools,
                stop_reason="REPORT_CONTEXT_UNAVAILABLE",
                safe_error_code="REPORT_CONTEXT_UNAVAILABLE",
            )


class _ReportingRun:
    def __init__(self, harness: ReportingHarness, handoff: AgentHandoff):
        self.harness, self.handoff = harness, handoff
        self.value: ReportingRequest | None = None
        self.instructions = ""
        self.deadline = monotonic() + min(180, handoff.budget.timeout_seconds)

    async def authorize(self) -> None:
        actor = await self.harness.actors.resolve(self.handoff.actor)
        PolicyGuard().authorize_handoff(
            current_actor=actor,
            parent_agent_id=AgentId.ORCHESTRATOR,
            handoff=self.handoff,
            manifest=self.harness.manifest,
            requested_tool_ids=self.harness.manifest.allowed_tools,
            requested_skill_ids=self.harness.manifest.allowed_skills,
        )
        if any(r.organization_id != actor.organization_id for r in self.handoff.context_references):
            raise ValueError("REPORTING_CONTEXT_TENANT")

    async def tool(
        self, state: ReportingState, name: str, payload: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        if state.tools >= self.handoff.budget.max_tool_calls:
            raise ValueError("REPORTING_TOOL_BUDGET")
        if f"{name}@1" not in self.harness.manifest.allowed_tools:
            raise ValueError("REPORTING_TOOL_DENIED")
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise ValueError("REPORTING_DEADLINE")
        if self.harness.usage is not None:
            await self.harness.usage.reserve_tool(f"{self.handoff.step_id}:{state.tools + 1}")
        state.tools += 1
        async with asyncio.timeout(min(15, remaining)):
            result = await self.harness.tools.execute(
                ToolExecutionRequest(
                    agent_run_id=uuid5(NAMESPACE_URL, f"agent-run:{self.handoff.idempotency_key}"),
                    tool_id=name,
                    tool_version="1.0.0",
                    call_id=f"{self.handoff.step_id}:{state.tools}",
                    actor=self.handoff.actor,
                    typed_input=payload,
                    idempotency_key=f"{self.handoff.idempotency_key}:{name}",
                )
            )
        if result.status != "SUCCEEDED" or any(
            r.organization_id != self.handoff.actor.organization_id for r in result.evidence
        ):
            raise ValueError("REPORTING_ACCESS_CHANGED")
        return result.typed_output

    async def model[OutputT: BaseModel](
        self,
        state: ReportingState,
        *,
        key: str,
        system: str,
        payload: object,
        schema: type[OutputT],
    ) -> OutputT:
        content = json.dumps({"UNTRUSTED_CONTEXT": payload}, ensure_ascii=False)
        inputs = (
            len(content.encode())
            + len(system.encode())
            + len(json.dumps(schema.model_json_schema()).encode())
            + 1024
        )
        outputs = min(
            3000 if schema is ReportingNarrative else 1000, self.handoff.budget.max_output_tokens
        )
        if outputs < 128:
            raise ValueError("REPORTING_OUTPUT_BUDGET")
        while True:
            remaining = self.deadline - monotonic()
            if (
                remaining <= 0
                or state.attempts >= self.handoff.budget.max_model_attempts
                or state.input_reserved + inputs > self.handoff.budget.max_input_tokens
                or state.output_reserved + outputs > self.handoff.budget.max_output_tokens
            ):
                raise ValueError("REPORTING_MODEL_BUDGET")
            if self.harness.usage is not None:
                await self.harness.usage.reserve(input_tokens=inputs, output_tokens=outputs)
            state.attempts += 1
            state.input_reserved += inputs
            state.output_reserved += outputs
            try:
                with reporting_attempt_scope(state.attempts):
                    async with asyncio.timeout(min(60, remaining)):
                        response = await self.harness.gateway.generate_structured(
                            StructuredModelRequest(
                                invocation_key=key,
                                messages=(
                                    ModelMessage(role="system", content=system),
                                    ModelMessage(role="user", content=content),
                                ),
                                output_schema=schema,
                                timeout_seconds=min(60, remaining),
                                max_output_tokens=outputs,
                            )
                        )
                if response.usage and (
                    response.usage.input_tokens > inputs or response.usage.output_tokens > outputs
                ):
                    raise ValueError("REPORTING_USAGE_EXCEEDS_RESERVATION")
                state.model_refs.append(response.model_ref)
                return schema.model_validate(response.parsed.model_dump(mode="json"))
            except (ModelTimeoutError, ModelRateLimitError, TimeoutError):
                if state.retry_used:
                    raise
                if self.harness.usage is not None:
                    await self.harness.usage.consume_retry()
                state.retry_used = True

    async def execute_node(self, node: str, state: ReportingState) -> None:
        if node == "authorize":
            await self.authorize()
            self.value = ReportingRequest.model_validate(self.handoff.typed_input)
            instructions: list[str] = []
            for ref in self.harness.manifest.allowed_skills:
                package = f"work_management_ai.skills.{ref.split('@')[0]}"
                skill = load_yaml_resource(package, "skill.yaml", SkillManifest)
                if AgentId.REPORTING not in skill.runnable_by_agents or not set(
                    skill.allowed_tools
                ).issubset(self.harness.manifest.allowed_tools):
                    raise ValueError("REPORTING_SKILL_DENIED")
                instructions.append(files(package).joinpath("SKILL.md").read_text())
            self.instructions = "\n".join(instructions)
            return
        value = self.value
        if value is None:
            raise ValueError("REPORTING_REQUEST_REQUIRED")
        if node == "read":
            context = ReportingContext.model_validate(
                await self.tool(
                    state,
                    "reporting.read",
                    cast(dict[str, JsonValue], value.model_dump(mode="json")),
                )
            )
            if (
                context.snapshot.organization_id != self.handoff.actor.organization_id
                or context.snapshot.report_id != value.report_id
                or context.base_version_id != value.base_version_id
                or context.snapshot.snapshot_hash != value.snapshot_hash
                or not context.snapshot.verified_hash()
                or context.locale != value.locale
            ):
                raise ValueError("REPORTING_SNAPSHOT_MISMATCH")
            state.context = context
            return
        context = state.context
        if context is None:
            raise ValueError("REPORTING_CONTEXT_REQUIRED")
        if node == "draft" and value.mode == "VERIFY_EDIT":
            if context.edited_narrative is None:
                raise ValueError("REPORTING_EDIT_REQUIRED")
            state.narrative = context.edited_narrative
            return
        if node == "draft":
            if context.edited_narrative is not None:
                raise ValueError("REPORTING_MODE_MISMATCH")
            state.narrative = await self.model(
                state,
                key=f"reporting.{value.locale}.draft",
                system=SYSTEM_V1
                + self.instructions
                + (
                    SUMMARY_V1
                    if context.snapshot.query_version == "daily-summary-conversion.v1"
                    else ""
                ),
                payload=model_context(context),
                schema=ReportingNarrative,
            )
            if state.narrative.locale != value.locale:
                raise ValueError("REPORTING_LOCALE_MISMATCH")
            return
        narrative = state.narrative
        if narrative is None:
            raise ValueError("REPORTING_NARRATIVE_REQUIRED")
        if node == "numeric":
            verdict = verify_numeric(
                context.snapshot, narrative, source_detail_limit=SOURCE_DETAIL_LIMIT
            )
            state.verifiers.append(verdict)
            if not verdict.passed:
                raise ValueError("REPORTING_NUMERIC_REJECTED")
        elif node == "semantic":
            state.semantic = await self.model(
                state,
                key=f"reporting.{value.locale}.grounding",
                system=GROUNDING_V1,
                payload={
                    "context": model_context(context),
                    "narrative": narrative.model_dump(mode="json"),
                },
                schema=SemanticVerdict,
            )
            verdict = verify_grounding(narrative, state.semantic)
            state.verifiers.append(verdict)
            if not verdict.passed:
                raise ValueError("REPORTING_SEMANTIC_REJECTED")
        elif node == "reauthorize":
            await self.authorize()
            current = ReportingContext.model_validate(
                await self.tool(
                    state,
                    "reporting.read",
                    cast(dict[str, JsonValue], value.model_dump(mode="json")),
                )
            )
            if current != context:
                raise ValueError("REPORTING_SOURCE_ACCESS_CHANGED")
        elif node == "propose":
            if state.semantic is None or not all(v.passed for v in state.verifiers):
                raise ValueError("REPORTING_VERIFICATION_REQUIRED")
            proposal = ReportingProposal(
                request=value,
                narrative=narrative,
                semantic_verdict=state.semantic,
                prompt_version="reporting.summary.v1"
                if context.snapshot.query_version == "daily-summary-conversion.v1"
                else "reporting.system.v1",
                manifest_fingerprint=canonical_manifest_fingerprint(self.harness.manifest),
                skill_versions=tuple(
                    f"{r.split('@')[0]}@1.0.0" for r in self.harness.manifest.allowed_skills
                ),
                tool_versions=("reporting.read@1.0.0", "reporting.propose@1.0.0"),
                model_refs=tuple(state.model_refs),
            )
            receipt = ProposalReceipt.model_validate(
                await self.tool(
                    state,
                    "reporting.propose",
                    cast(dict[str, JsonValue], proposal.model_dump(mode="json")),
                )
            )
            state.proposal_id = str(receipt.version_id)
