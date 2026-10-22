"""Only explicit nodes; source/model text cannot add edges or tools."""

from dataclasses import dataclass, field
from itertools import pairwise
from typing import Protocol
from uuid import UUID

from work_management_ai.agents.retrieve.contracts import (
    GroundedSummary,
    RetrieveOutcome,
    WebEvidence,
)
from work_management_ai.agents.retrieve.evidence import EvidencePack
from work_management_ai.runtime.contracts import ContextReference, VerifierResult

NODES = ("validate", "source_policy", "search", "read", "pack", "synthesize", "verify", "finish")
EDGES = tuple(pairwise(NODES))
RETRY_LIMIT = 1
APPROVAL_POINT = None  # Read-only; no approval state or business mutation can be produced.
STOP_CONDITIONS = (
    "inactive_capability",
    "actor_revoked",
    "source_expired",
    "cancelled",
    "budget_exhausted",
)
FALLBACK = "verified_evidence_only"


@dataclass
class RetrieveState:
    sources: list[WebEvidence] = field(default_factory=lambda: list[WebEvidence]())
    references: dict[UUID, ContextReference] = field(
        default_factory=lambda: dict[UUID, ContextReference]()
    )
    pack: EvidencePack | None = None
    summary: GroundedSummary | None = None
    verdict: VerifierResult | None = None
    outcome: RetrieveOutcome | None = None
    question: str | None = None
    stopped: bool = False


class NodeHandlers(Protocol):
    async def execute_node(self, node: str, state: RetrieveState) -> None: ...


class RetrieveGraph:
    async def run(self, handlers: NodeHandlers, state: RetrieveState) -> RetrieveState:
        for node in NODES:
            if state.stopped:
                break
            await handlers.execute_node(node, state)
        return state
