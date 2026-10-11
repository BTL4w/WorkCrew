"""Bounded-agentic LangGraph workflow for Phase 2 planning proposals."""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false

from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, Interrupt, interrupt
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from work_management_ai.model_gateway.contracts import (
    ModelGateway,
    StructuredModelRequest,
)
from work_management_ai.model_gateway.errors import ModelGatewayError, ModelInvalidOutputError
from work_management_ai.prompts.planning import build_planning_messages, build_revision_messages
from work_management_ai.schemas.planning import PlanningModelOutput
from work_management_ai.tracing import NoopTracePort, TraceMetadata, TracePort, record_safely
from work_management_ai.workflows.planning.context import (
    PermittedPlanningContext,
    PlanningContextPort,
    PlanningContextRequest,
)
from work_management_ai.workflows.planning.policy import evaluate_planning_policy
from work_management_ai.workflows.planning.ports import (
    PlanningCheckpoint,
    PlanningPersistencePort,
    PlanningProgressEvent,
    PlanningProposalDraft,
    PlanningRevisionBase,
    PlanningRevisionDraft,
    ProgressivePlanningPersistencePort,
)
from work_management_ai.workflows.planning.state import (
    PlanningRevisionError,
    PlanningState,
    ProgressivePlanningResult,
    ProgressivePlanningState,
    checkpoint_state,
    merge_revision_assignees,
)
from work_management_ai.workflows.planning.verifier import (
    PlanningValidationItem,
    PlanningValidationResult,
    PlanningVerificationContext,
    verify_plan,
)

NODES = (
    "policy_and_scope_guard",
    "load_permitted_context",
    "planning_agent",
    "generate_structured_plan",
    "validate_schema",
    "deterministic_verifier",
    "persist_proposal",
    "await_manager_input",
    "await_manager_decision",
    "manual_fallback",
)


@dataclass(frozen=True, slots=True)
class PlanningInterrupt:
    kind: str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class PlanningGraphResult:
    state: PlanningState
    interrupt: PlanningInterrupt | None


class _RevisionModelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content: PlanningModelOutput
    change_summary: str = Field(min_length=1, max_length=4_000)


class PlanningGraph:
    """One fixed planning graph with bounded model repair and revision loops."""

    def __init__(
        self,
        *,
        model_gateway: ModelGateway,
        context_port: PlanningContextPort,
        persistence_port: PlanningPersistencePort,
        trace_port: TracePort | None = None,
        progressive_persistence_port: ProgressivePlanningPersistencePort | None = None,
    ) -> None:
        self._progressive_persistence_port = progressive_persistence_port
        self._model_gateway = model_gateway
        self._context_port = context_port
        self._persistence_port = persistence_port
        self._trace_port = trace_port or NoopTracePort()
        self._compiled = self._compile()

    async def run_progressive(self, state: ProgressivePlanningState) -> ProgressivePlanningResult:
        from work_management_ai.workflows.planning.progressive_graph import run_progressive

        if self._progressive_persistence_port is None:
            raise RuntimeError("progressive persistence port is required")
        return await run_progressive(
            state,
            gateway=self._model_gateway,
            context_port=self._context_port,
            persistence=self._progressive_persistence_port,
        )

    @staticmethod
    def thread_id(run_id: UUID) -> str:
        return str(run_id)

    async def run(self, state: PlanningState) -> PlanningGraphResult:
        raw = await self._compiled.ainvoke(
            state,
            config={"configurable": {"thread_id": self.thread_id(state["run_id"])}},
        )
        return self._result(raw)

    async def resume(self, run_id: UUID, manager_value: str) -> PlanningGraphResult:
        raw = await self._compiled.ainvoke(
            Command(resume=manager_value),
            config={"configurable": {"thread_id": self.thread_id(run_id)}},
        )
        return self._result(raw)

    async def resume_from_checkpoint(
        self,
        persisted_state: dict[str, object],
        manager_value: str,
    ) -> PlanningGraphResult:
        """Resume a manager-input interrupt from a persisted typed checkpoint."""

        state = TypeAdapter(PlanningState).validate_python(persisted_state)
        if state["stage"] != "NEEDS_INPUT" or not state["pending_questions"]:
            raise ValueError("checkpoint is not awaiting manager input")
        answer = manager_value.strip()
        if not answer:
            raise ValueError("manager input must not be blank")
        restored = cast(
            PlanningState,
            {
                **state,
                "stage": "UNDERSTANDING",
                "manager_answers": (*state["manager_answers"], answer),
                "pending_questions": state["pending_questions"][1:],
            },
        )
        config: RunnableConfig = {"configurable": {"thread_id": self.thread_id(state["run_id"])}}
        await self._compiled.aupdate_state(
            config,
            restored,
            as_node="await_manager_input",
        )
        raw = await self._compiled.ainvoke(None, config=config)
        return self._result(raw)

    async def generate_revision(
        self,
        *,
        base: PlanningRevisionBase,
        instruction: str,
        context: PermittedPlanningContext,
    ) -> PlanningRevisionDraft:
        """Generate and verify a revision without invoking any persistence port."""

        normalized_instruction = instruction.strip()
        if not normalized_instruction or len(normalized_instruction) > 8_000:
            raise PlanningRevisionError("REVISION_INSTRUCTION_INVALID")
        request = StructuredModelRequest(
            invocation_key=f"planning.{base.locale}.proposal_revision",
            messages=build_revision_messages(
                locale=base.locale,
                base=base.content,
                instruction=normalized_instruction,
                structured_context=context.structured_facts,
            ),
            output_schema=_RevisionModelOutput,
            timeout_seconds=120,
        )
        response = await self._model_gateway.generate_structured(request)
        merged = merge_revision_assignees(base.content, response.parsed.content)
        validation = verify_plan(
            merged,
            PlanningVerificationContext(),
        )
        invalid = tuple(item for item in validation.errors if item.code != "ASSIGNEE_REQUIRED")
        if invalid:
            raise PlanningRevisionError(invalid[0].code)
        return PlanningRevisionDraft(
            base_proposal_id=base.proposal_id,
            base_version=base.version,
            content=merged,
            change_summary=response.parsed.change_summary,
            model_reference=response.model_ref,
        )

    def _compile(self) -> CompiledStateGraph[PlanningState, None, PlanningState, PlanningState]:
        builder = StateGraph(PlanningState)
        builder.add_node("policy_and_scope_guard", self._policy_and_scope_guard)
        builder.add_node("load_permitted_context", self._load_permitted_context)
        builder.add_node("planning_agent", self._planning_agent)
        builder.add_node("generate_structured_plan", self._generate_structured_plan)
        builder.add_node("validate_schema", self._validate_schema)
        builder.add_node("deterministic_verifier", self._deterministic_verifier)
        builder.add_node("persist_proposal", self._persist_proposal)
        builder.add_node("await_manager_input", self._await_manager_input)
        builder.add_node("await_manager_decision", self._await_manager_decision)
        builder.add_node("manual_fallback", self._manual_fallback)
        builder.add_edge(START, "policy_and_scope_guard")
        builder.add_conditional_edges(
            "policy_and_scope_guard",
            self._route_after_policy,
            {"context": "load_permitted_context", "terminal": END},
        )
        builder.add_edge("load_permitted_context", "planning_agent")
        builder.add_conditional_edges(
            "planning_agent",
            self._route_after_planning,
            {"input": "await_manager_input", "generate": "generate_structured_plan"},
        )
        builder.add_edge("await_manager_input", "planning_agent")
        builder.add_edge("generate_structured_plan", "validate_schema")
        builder.add_conditional_edges(
            "validate_schema",
            self._route_after_schema,
            {
                "repair": "generate_structured_plan",
                "verify": "deterministic_verifier",
                "fallback": "manual_fallback",
            },
        )
        builder.add_conditional_edges(
            "deterministic_verifier",
            self._route_after_verifier,
            {"revise": "generate_structured_plan", "persist": "persist_proposal"},
        )
        builder.add_edge("persist_proposal", "await_manager_decision")
        builder.add_edge("await_manager_decision", END)
        builder.add_edge("manual_fallback", END)
        return builder.compile(checkpointer=InMemorySaver())

    async def _policy_and_scope_guard(self, state: PlanningState) -> dict[str, object]:
        decision = evaluate_planning_policy(
            actor_role=state["actor_role"],
            user_brief=state["user_brief"],
        )
        if decision.outcome == "ALLOW":
            updates: dict[str, object] = {"stage": "POLICY_CHECKED"}
        else:
            code = decision.code or "UNSUPPORTED_CAPABILITY"
            updates = {
                "stage": decision.outcome,
                "validation_result": _single_error(code),
            }
        await self._record_node(state, "policy_and_scope_guard", updates)
        return updates

    async def _load_permitted_context(self, state: PlanningState) -> dict[str, object]:
        context = await self._context(state)
        updates: dict[str, object] = {
            "stage": "CONTEXT_LOADED",
            "context_reference_ids": context.reference_ids,
            "pending_questions": context.required_questions,
        }
        await self._record_node(state, "load_permitted_context", updates)
        return updates

    async def _planning_agent(self, state: PlanningState) -> dict[str, object]:
        unanswered = state["pending_questions"]
        if unanswered:
            updates: dict[str, object] = {
                "stage": "NEEDS_INPUT",
                "understanding": state["user_brief"].strip(),
            }
        else:
            updates = {
                "stage": "GENERATING",
                "understanding": state["user_brief"].strip(),
            }
        await self._record_node(state, "planning_agent", updates)
        return updates

    async def _await_manager_input(self, state: PlanningState) -> dict[str, object]:
        await self._record_node(state, "await_manager_input", {})
        question = state["pending_questions"][0]
        answer = cast(
            str,
            interrupt(
                {
                    "kind": "MANAGER_INPUT_REQUIRED",
                    "question": question,
                    "remaining_questions": len(state["pending_questions"]),
                }
            ),
        )
        return {
            "stage": "UNDERSTANDING",
            "manager_answers": (*state["manager_answers"], answer),
            "pending_questions": state["pending_questions"][1:],
        }

    async def _generate_structured_plan(self, state: PlanningState) -> dict[str, object]:
        mode: Literal["generate", "repair", "revision"] = "generate"
        if state["verifier_revision_count"]:
            mode = "revision"
        elif state["schema_repair_count"]:
            mode = "repair"
        context = await self._context(state)
        validation_codes = (
            tuple(item.code for item in state["validation_result"].errors)
            if state["validation_result"] is not None
            else ()
        )
        request = StructuredModelRequest(
            invocation_key=f"planning.{state['locale']}.{mode}",
            messages=build_planning_messages(
                locale=state["locale"],
                structured_context=context.structured_facts,
                user_brief=state["user_brief"],
                manager_answers=state["manager_answers"],
                mode=mode,
                validation_codes=validation_codes,
            ),
            output_schema=PlanningModelOutput,
            timeout_seconds=120,
        )
        try:
            response = await self._model_gateway.generate_structured(request)
            proposal = response.parsed.model_copy(
                update={
                    "tasks": [
                        task.model_copy(update={"assignee_membership_id": None})
                        for task in response.parsed.tasks
                    ]
                }
            )
            updates: dict[str, object] = {
                "stage": "SCHEMA_VALIDATING",
                "proposal": proposal,
                "schema_error_code": None,
                "model_reference": response.model_ref,
            }
        except ModelInvalidOutputError:
            updates = {
                "stage": "SCHEMA_VALIDATING",
                "proposal": None,
                "schema_error_code": "MODEL_INVALID_OUTPUT",
            }
        except ModelGatewayError:
            updates = {
                "stage": "MANUAL_FALLBACK",
                "proposal": None,
                "schema_error_code": "MODEL_UNAVAILABLE",
            }
        await self._record_node(state, "generate_structured_plan", updates)
        return updates

    async def _validate_schema(self, state: PlanningState) -> dict[str, object]:
        if state["proposal"] is not None:
            updates: dict[str, object] = {"stage": "VERIFYING"}
        elif (
            state["schema_error_code"] == "MODEL_INVALID_OUTPUT"
            and not state["schema_repair_count"]
        ):
            updates = {"stage": "GENERATING", "schema_repair_count": 1}
        else:
            updates = {
                "stage": "MANUAL_FALLBACK",
                "validation_result": _single_error(
                    state["schema_error_code"] or "MODEL_INVALID_OUTPUT"
                ),
            }
        await self._record_node(state, "validate_schema", updates)
        return updates

    async def _deterministic_verifier(self, state: PlanningState) -> dict[str, object]:
        proposal = state["proposal"]
        if proposal is None:
            raise RuntimeError("schema-validated proposal is required")
        validation = verify_plan(
            proposal,
            PlanningVerificationContext(),
        )
        repairable_errors = validation.errors
        if repairable_errors and not state["verifier_revision_count"]:
            updates: dict[str, object] = {
                "stage": "GENERATING",
                "validation_result": validation,
                "verifier_revision_count": 1,
                "proposal": None,
            }
        else:
            updates = {
                "stage": "PERSISTING_PROPOSAL",
                "validation_result": validation,
            }
        await self._record_node(state, "deterministic_verifier", updates)
        return updates

    async def _persist_proposal(self, state: PlanningState) -> dict[str, object]:
        proposal = state["proposal"]
        validation = state["validation_result"]
        if proposal is None or validation is None:
            raise RuntimeError("proposal and validation are required for persistence")
        reference = await self._persistence_port.persist_proposal(
            PlanningProposalDraft(
                idempotency_key=f"{state['run_id']}:proposal:1",
                run_id=state["run_id"],
                organization_id=state["organization_id"],
                actor_membership_id=state["actor_membership_id"],
                content=proposal,
                validation=validation,
                context_reference_ids=state["context_reference_ids"],
                workflow_version=state["workflow_version"],
                prompt_version=state["prompt_version"],
                schema_version=state["schema_version"],
                model_reference=state["model_reference"] or "UNKNOWN",
                verifier_version=state["verifier_version"],
            )
        )
        updates: dict[str, object] = {
            "stage": "WAITING_FOR_DECISION",
            "proposal_id": reference.proposal_id,
            "proposal_version": reference.version,
            "assumptions": tuple(item.description for item in proposal.assumptions),
        }
        await self._record_node(state, "persist_proposal", updates)
        return updates

    async def _await_manager_decision(self, state: PlanningState) -> dict[str, object]:
        await self._record_node(state, "await_manager_decision", {})
        decision = cast(
            str,
            interrupt(
                {
                    "kind": "MANAGER_DECISION_REQUIRED",
                    "proposal_id": str(state["proposal_id"]),
                    "proposal_version": state["proposal_version"],
                    "can_approve": (
                        state["validation_result"].can_approve
                        if state["validation_result"] is not None
                        else False
                    ),
                }
            ),
        )
        # Task 8 owns decision application. Resuming here only records a safe signal.
        return {"stage": "DECISION_RECEIVED", "understanding": decision[:200]}

    async def _manual_fallback(self, state: PlanningState) -> dict[str, object]:
        updates: dict[str, object] = {"stage": "MANUAL_FALLBACK", "proposal": None}
        await self._record_node(state, "manual_fallback", updates)
        return updates

    async def _context(self, state: PlanningState) -> PermittedPlanningContext:
        return await self._context_port.load_permitted_context(
            PlanningContextRequest(
                run_id=state["run_id"],
                organization_id=state["organization_id"],
                actor_membership_id=state["actor_membership_id"],
                locale=state["locale"],
                user_brief=state["user_brief"],
                manager_answers=state["manager_answers"],
            )
        )

    async def _record_node(
        self,
        state: PlanningState,
        node: str,
        updates: dict[str, object],
    ) -> None:
        merged = cast(PlanningState, {**state, **updates})
        key = self._node_key(merged, node)
        await self._persistence_port.save_checkpoint(
            PlanningCheckpoint(
                idempotency_key=key,
                thread_id=self.thread_id(state["run_id"]),
                run_id=state["run_id"],
                organization_id=state["organization_id"],
                node=node,
                state=checkpoint_state(merged),
            )
        )
        await self._persistence_port.append_progress(
            PlanningProgressEvent(
                idempotency_key=key,
                run_id=state["run_id"],
                organization_id=state["organization_id"],
                stage=merged["stage"],
                public_payload={"node": node, "stage": merged["stage"]},
            )
        )
        await record_safely(
            self._trace_port,
            TraceMetadata(
                run_id=state["run_id"],
                node=node,
                outcome="COMPLETED",
                workflow_version=state["workflow_version"],
                prompt_version=state["prompt_version"],
                schema_version=state["schema_version"],
                verifier_version=state["verifier_version"],
                model_reference=state["model_reference"],
                error_code=merged.get("schema_error_code"),
            ),
        )

    @staticmethod
    def _node_key(state: PlanningState, node: str) -> str:
        return (
            f"{state['run_id']}:{node}:a{len(state['manager_answers'])}:"
            f"s{state['schema_repair_count']}:v{state['verifier_revision_count']}"
        )

    @staticmethod
    async def _route_after_policy(state: PlanningState) -> Literal["context", "terminal"]:
        return "context" if state["stage"] == "POLICY_CHECKED" else "terminal"

    @staticmethod
    async def _route_after_planning(state: PlanningState) -> Literal["input", "generate"]:
        return "input" if state["stage"] == "NEEDS_INPUT" else "generate"

    @staticmethod
    async def _route_after_schema(
        state: PlanningState,
    ) -> Literal["repair", "verify", "fallback"]:
        if state["stage"] == "VERIFYING":
            return "verify"
        if state["stage"] == "GENERATING":
            return "repair"
        return "fallback"

    @staticmethod
    async def _route_after_verifier(state: PlanningState) -> Literal["revise", "persist"]:
        return "revise" if state["stage"] == "GENERATING" else "persist"

    @staticmethod
    def _result(raw: object) -> PlanningGraphResult:
        output = cast(dict[str, object], raw)
        raw_interrupts = cast(tuple[Interrupt, ...], output.pop("__interrupt__", ()))
        graph_interrupt: PlanningInterrupt | None = None
        if raw_interrupts:
            value = cast(dict[str, object], raw_interrupts[0].value)
            kind = cast(str, value.pop("kind"))
            graph_interrupt = PlanningInterrupt(kind=kind, payload=value)
        return PlanningGraphResult(
            state=TypeAdapter(PlanningState).validate_python(output),
            interrupt=graph_interrupt,
        )


def _single_error(code: str) -> PlanningValidationResult:
    return PlanningValidationResult(
        errors=(
            PlanningValidationItem(
                path="request",
                code=code,
                message_key=f"planning.validation.{code.casefold()}",
                severity="ERROR",
            ),
        ),
        warnings=(),
    )
