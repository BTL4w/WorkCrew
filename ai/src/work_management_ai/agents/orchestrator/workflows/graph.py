"""Stable LangGraph topology for the Orchestrator Harness."""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false

from typing import Literal, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from work_management_ai.agents.orchestrator.contracts import (
    ExecutionPlan,
    OrchestratorInput,
    OrchestratorOutput,
    OrchestratorStatus,
    OrchestratorTriggerInput,
)
from work_management_ai.agents.risk.contracts import RiskReplanRequest
from work_management_ai.runtime.contracts import (
    AgentHandoff,
    AgentResult,
    RequestedHandoff,
    ResolvedActorContext,
    ResponseBlock,
)

NODES = (
    "intake_turn",
    "build_context",
    "prepare_schedule",
    "plan_objective",
    "validate_execution_plan",
    "select_next_step",
    "delegate_specialist",
    "observe_and_update_plan",
    "synthesize",
    "verify_response",
    "persistable_result",
    "ask_user",
    "human_gate",
    "capability_unavailable",
    "bounded_repair",
    "manual_fallback",
)

type Route = Literal[
    "schedule",
    "execute",
    "delegate",
    "next",
    "synthesize",
    "repair",
    "replan",
    "ask_user",
    "human_gate",
    "capability_unavailable",
    "manual_fallback",
]


class OrchestratorState(TypedDict):
    value: OrchestratorInput
    risk_replan: RiskReplanRequest | None
    current_actor: ResolvedActorContext | None
    plan: ExecutionPlan | None
    prior_plan: ExecutionPlan | None
    original_plan: ExecutionPlan | None
    results: tuple[AgentResult, ...]
    completed_step_ids: tuple[str, ...]
    last_batch_handoffs: tuple[AgentHandoff, ...]
    last_batch_results: tuple[AgentResult, ...]
    blocks: tuple[ResponseBlock, ...]
    status: OrchestratorStatus
    stop_reason: str
    route: Route
    repair_attempts: int
    replans_used: int
    handoffs_used: int
    model_refs: tuple[str, ...]
    pending_requested_handoff: RequestedHandoff | None
    output: OrchestratorOutput | None


class OrchestratorNodeHandlers(Protocol):
    async def intake_turn(self, state: OrchestratorState) -> dict[str, object]: ...

    async def build_context(self, state: OrchestratorState) -> dict[str, object]: ...

    async def prepare_schedule(self, state: OrchestratorState) -> dict[str, object]: ...

    async def plan_objective(self, state: OrchestratorState) -> dict[str, object]: ...

    async def validate_plan(self, state: OrchestratorState) -> dict[str, object]: ...

    async def select_next_step(self, state: OrchestratorState) -> dict[str, object]: ...

    async def delegate_specialist(self, state: OrchestratorState) -> dict[str, object]: ...

    async def observe_and_update_plan(self, state: OrchestratorState) -> dict[str, object]: ...

    async def synthesize(self, state: OrchestratorState) -> dict[str, object]: ...

    async def verify_response(self, state: OrchestratorState) -> dict[str, object]: ...

    async def persistable_result(self, state: OrchestratorState) -> dict[str, object]: ...

    async def ask_user(self, state: OrchestratorState) -> dict[str, object]: ...

    async def human_gate(self, state: OrchestratorState) -> dict[str, object]: ...

    async def capability_unavailable(self, state: OrchestratorState) -> dict[str, object]: ...

    async def bounded_repair(self, state: OrchestratorState) -> dict[str, object]: ...

    async def manual_fallback(self, state: OrchestratorState) -> dict[str, object]: ...


class OrchestratorGraph:
    def __init__(self, handlers: OrchestratorNodeHandlers) -> None:
        self._compiled = self._compile(handlers)

    async def run(self, state: OrchestratorState) -> OrchestratorOutput:
        result = await self._compiled.ainvoke(state)
        output = result["output"]
        if output is None:
            raise RuntimeError("orchestrator graph completed without an output")
        return output

    @staticmethod
    def _compile(
        handlers: OrchestratorNodeHandlers,
    ) -> CompiledStateGraph[OrchestratorState, None, OrchestratorState, OrchestratorState]:
        builder = StateGraph(OrchestratorState)
        builder.add_node("intake_turn", handlers.intake_turn)
        builder.add_node("build_context", handlers.build_context)
        builder.add_node("prepare_schedule", handlers.prepare_schedule)
        builder.add_node("plan_objective", handlers.plan_objective)
        builder.add_node("validate_execution_plan", handlers.validate_plan)
        builder.add_node("select_next_step", handlers.select_next_step)
        builder.add_node("delegate_specialist", handlers.delegate_specialist)
        builder.add_node("observe_and_update_plan", handlers.observe_and_update_plan)
        builder.add_node("synthesize", handlers.synthesize)
        builder.add_node("verify_response", handlers.verify_response)
        builder.add_node("persistable_result", handlers.persistable_result)
        builder.add_node("ask_user", handlers.ask_user)
        builder.add_node("human_gate", handlers.human_gate)
        builder.add_node("capability_unavailable", handlers.capability_unavailable)
        builder.add_node("bounded_repair", handlers.bounded_repair)
        builder.add_node("manual_fallback", handlers.manual_fallback)

        builder.add_edge(START, "intake_turn")
        builder.add_conditional_edges(
            "intake_turn",
            _route,
            {"execute": "build_context", "manual_fallback": "manual_fallback"},
        )
        builder.add_conditional_edges(
            "build_context",
            _route,
            {"execute": "plan_objective", "ask_user": "ask_user", "schedule": "prepare_schedule"},
        )
        builder.add_conditional_edges(
            "prepare_schedule",
            _route,
            {"execute": "persistable_result", "manual_fallback": "manual_fallback"},
        )
        builder.add_edge("plan_objective", "validate_execution_plan")
        builder.add_conditional_edges(
            "validate_execution_plan",
            _route,
            {
                "execute": "select_next_step",
                "repair": "bounded_repair",
                "capability_unavailable": "capability_unavailable",
                "manual_fallback": "manual_fallback",
            },
        )
        builder.add_edge("bounded_repair", "plan_objective")
        builder.add_conditional_edges(
            "select_next_step",
            _route,
            {"delegate": "delegate_specialist", "synthesize": "synthesize"},
        )
        builder.add_conditional_edges(
            "delegate_specialist",
            _route,
            {"execute": "observe_and_update_plan", "manual_fallback": "manual_fallback"},
        )
        builder.add_conditional_edges(
            "observe_and_update_plan",
            _route,
            {
                "next": "select_next_step",
                "replan": "bounded_repair",
                "ask_user": "ask_user",
                "human_gate": "human_gate",
                "manual_fallback": "manual_fallback",
            },
        )
        builder.add_conditional_edges(
            "synthesize",
            _route,
            {"execute": "verify_response", "manual_fallback": "manual_fallback"},
        )
        builder.add_edge("verify_response", "persistable_result")
        builder.add_edge("ask_user", "persistable_result")
        builder.add_edge("human_gate", "persistable_result")
        builder.add_edge("capability_unavailable", "persistable_result")
        builder.add_edge("manual_fallback", "persistable_result")
        builder.add_edge("persistable_result", END)
        return builder.compile()


def _route(state: OrchestratorState) -> Route:
    return state["route"]


TRIGGER_NODES = ("resolve_and_dispatch_trigger", "typed_result")


class TriggerState(TypedDict):
    value: OrchestratorTriggerInput
    output: OrchestratorOutput | None


class TriggerDispatcher(Protocol):
    async def dispatch_trigger(self, value: OrchestratorTriggerInput) -> OrchestratorOutput: ...


class OrchestratorTriggerGraph:
    """Deterministic typed path: one guarded handoff, zero intent calls/replans/retries."""

    def __init__(self, dispatcher: TriggerDispatcher):
        self._dispatcher = dispatcher
        builder = StateGraph(TriggerState)
        builder.add_node("resolve_and_dispatch_trigger", self.dispatch)
        builder.add_node("typed_result", self.result)
        builder.add_edge(START, "resolve_and_dispatch_trigger")
        builder.add_edge("resolve_and_dispatch_trigger", "typed_result")
        builder.add_edge("typed_result", END)
        self._compiled = builder.compile()

    async def dispatch(self, state: TriggerState) -> dict[str, object]:
        return {"output": await self._dispatcher.dispatch_trigger(state["value"])}

    @staticmethod
    def result(state: TriggerState) -> dict[str, object]:
        if state["output"] is None:
            raise RuntimeError("TRIGGER_OUTPUT_MISSING")
        return {"output": state["output"]}

    async def run(self, value: OrchestratorTriggerInput) -> OrchestratorOutput:
        result = await self._compiled.ainvoke(
            TriggerState(value=value, output=None), config={"recursion_limit": 8}
        )
        output = result["output"]
        if output is None:
            raise RuntimeError("TRIGGER_OUTPUT_MISSING")
        return output
