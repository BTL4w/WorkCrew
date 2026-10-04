"""Seven bounded steps; one transient retry per run, proposal is the human gate."""

from dataclasses import dataclass, field
from itertools import pairwise
from typing import Protocol

from work_management_ai.agents.reporting.contracts import (
    ReportingContext,
    ReportingNarrative,
    SemanticVerdict,
)
from work_management_ai.runtime.contracts import VerifierResult

NODES = ("authorize", "read", "draft", "numeric", "semantic", "reauthorize", "propose")
EDGES = tuple(pairwise(NODES))
RETRY_LIMIT = 1
APPROVAL_POINT = "Manager/Admin reviews exact proposed version before publication"
STOP_CONDITIONS = (
    "invalid_handoff",
    "budget_exhausted",
    "access_changed",
    "verifier_rejected",
    "awaiting_manager_review",
)
FALLBACK = "metrics_only"


@dataclass
class ReportingState:
    context: ReportingContext | None = None
    narrative: ReportingNarrative | None = None
    semantic: SemanticVerdict | None = None
    verifiers: list[VerifierResult] = field(default_factory=lambda: list[VerifierResult]())
    model_refs: list[str] = field(default_factory=lambda: list[str]())
    iterations: int = 0
    tools: int = 0
    attempts: int = 0
    input_reserved: int = 0
    output_reserved: int = 0
    retry_used: bool = False
    proposal_id: str | None = None


class NodeHandlers(Protocol):
    async def execute_node(self, node: str, state: ReportingState) -> None: ...


class ReportingGraph:
    async def run(
        self, handlers: NodeHandlers, state: ReportingState, maximum: int
    ) -> ReportingState:
        for node in NODES:
            if state.iterations >= maximum:
                raise ValueError("REPORTING_ITERATION_BUDGET")
            state.iterations += 1
            await handlers.execute_node(node, state)
        return state
