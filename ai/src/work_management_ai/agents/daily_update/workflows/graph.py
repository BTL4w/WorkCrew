"""Typed, bounded Daily Update graph; the product API owns the human gate."""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
from typing import Literal, Protocol, TypedDict, cast

from langgraph.graph import END, START, StateGraph

from work_management_ai.agents.daily_update.contracts import (
    DailyUpdateHandoff,
    DailyUpdateResult,
    ExtractedReport,
)
from work_management_ai.runtime.contracts import AgentHandoff, AgentResult, AgentRunStatus
from work_management_ai.tools.daily_update.contracts import DailyUpdateToolInput

NODES = ("authorize", "context", "extract_report", "save_draft", "assess_originals", "await_owner")
RETRY_LIMIT = 1
STOP_CONDITIONS = (
    "invalid_contract",
    "budget_exhausted",
    "manual_fallback",
    "needs_input",
    "await_owner",
)


class DailyUpdateState(TypedDict):
    handoff: AgentHandoff
    value: DailyUpdateHandoff | None
    tool_input: DailyUpdateToolInput | None
    report: ExtractedReport | None
    card: DailyUpdateResult | None
    instructions: str
    iterations: int
    model_attempts: int
    tool_calls: int
    status: AgentRunStatus
    stopped: bool
    result: AgentResult | None


class NodeHandlers(Protocol):
    async def execute_node(self, node: str, state: DailyUpdateState) -> None: ...


class GraphNode(Protocol):
    async def __call__(self, state: DailyUpdateState) -> dict[str, object]: ...


def route(state: DailyUpdateState) -> Literal["stop", "continue"]:
    return "stop" if state["stopped"] else "continue"


class DailyUpdateGraph:
    def __init__(self, handlers: NodeHandlers) -> None:
        self.handlers = handlers
        graph = StateGraph(DailyUpdateState)

        def node_handler(node: str) -> GraphNode:
            async def execute(state: DailyUpdateState) -> dict[str, object]:
                try:
                    if state["iterations"] >= state["handoff"].budget.max_iterations:
                        raise ValueError("DAILY_UPDATE_ITERATION_BUDGET")
                    state["iterations"] += 1
                    await self.handlers.execute_node(node, state)
                except Exception:
                    state["status"] = AgentRunStatus.FAILED
                    state["stopped"] = True
                return dict(state)

            return execute

        for node in NODES:
            graph.add_node(node, node_handler(node))
        graph.add_edge(START, NODES[0])
        for index, node in enumerate(NODES[:-1]):
            next_node = NODES[index + 1]
            graph.add_conditional_edges(
                node,
                route,
                {"stop": END, "continue": next_node},
            )
        graph.add_edge(NODES[-1], END)
        self.graph = graph.compile()

    async def run(self, state: DailyUpdateState) -> DailyUpdateState:
        return cast(
            DailyUpdateState, await self.graph.ainvoke(state, config={"recursion_limit": 16})
        )
