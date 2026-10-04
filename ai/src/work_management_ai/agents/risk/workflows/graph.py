"""Four bounded nodes, no retry, no write tools or human gate."""

from dataclasses import dataclass
from typing import Protocol

from work_management_ai.agents.risk.contracts import RiskExplanation, RiskExplanationInput

NODES = ("authorize", "read", "explain", "revalidate")
RETRY_LIMIT = 0
STOP_CONDITIONS = ("invalid_handoff", "budget_exhausted", "access_changed", "complete")


@dataclass
class RiskState:
    context: RiskExplanationInput | None = None
    explanation: RiskExplanation | None = None
    fallback: bool = False
    iterations: int = 0
    tools: int = 0
    attempts: int = 0


class NodeHandlers(Protocol):
    async def execute_node(self, node: str, state: RiskState) -> None: ...


class RiskGraph:
    async def run(self, handlers: NodeHandlers, state: RiskState, maximum: int) -> RiskState:
        for node in NODES:
            if state.iterations >= maximum:
                raise ValueError("RISK_ITERATION_BUDGET")
            state.iterations += 1
            await handlers.execute_node(node, state)
        return state
