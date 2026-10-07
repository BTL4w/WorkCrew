"""Metadata-only, allowlisted operational payload retention contracts."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class PayloadClass(StrEnum):
    RAW_CONTEXT = "RAW_CONTEXT"
    REDACTED_TRACE = "REDACTED_TRACE"


# Static adapter map, never caller-provided SQL. Entries identify real persisted copies.
PAYLOAD_FIELDS: dict[str, dict[str, object]] = {
    "assistant_turns": {"objective": ""},
    "orchestration_runs": {"execution_plan": {}, "checkpoint": {}},
    "agent_runs": {"typed_input": {}, "typed_output": None},
    "agent_handoffs": {"objective": "", "typed_input": {}, "context_references": []},
    "agent_checkpoints": {"typed_state": {}},
    "skill_invocations": {"typed_input": {}, "typed_output": None},
    "tool_invocations": {"typed_input": {}, "typed_output": None, "context_references": []},
    "assistant_events": {"public_payload": {}},
    "assistant_jobs": {"payload": {}},
    "workflow_runs": {"input_goal_text": "", "error_message": None},
    "workflow_checkpoints": {"state": {}},
    "workflow_events": {"public_payload": {}},
    "workflow_jobs": {"payload": {}, "last_error": None},
    "context_references": {"provenance_notes": None},
    "evidence_processing_jobs": {"result": None},
    "evidence_segments": {"content": {}},
    "evaluation_candidates": {"context": None},
}


def expires_at(created_at: datetime, classification: PayloadClass, days: int) -> datetime:
    ceiling = 30 if classification == PayloadClass.RAW_CONTEXT else 90
    if created_at.tzinfo is None or not 0 <= days <= ceiling:
        raise ValueError("INVALID_RETENTION_POLICY")
    return created_at + timedelta(days=days)


@dataclass(frozen=True)
class PayloadLocator:
    organization_id: UUID
    storage_kind: str
    owner_id: UUID
    classification: PayloadClass
    created_at: datetime
    expires_at: datetime
    run_id: UUID | None
    fence: int

    def __post_init__(self) -> None:
        if self.storage_kind not in PAYLOAD_FIELDS:
            raise ValueError("UNKNOWN_PAYLOAD_STORAGE")
        ceiling = 30 if self.classification == PayloadClass.RAW_CONTEXT else 90
        if self.expires_at < self.created_at or self.expires_at > expires_at(
            self.created_at, self.classification, ceiling
        ):
            raise ValueError("INVALID_RETENTION_POLICY")


@dataclass(frozen=True)
class PurgeRecord:
    owner_id: UUID
    fence: int


@dataclass(frozen=True)
class PurgeResult:
    purged: int


class RetentionPayloadPort(Protocol):
    async def claim_expired(
        self, organization_id: UUID, now: datetime, limit: int
    ) -> tuple[PayloadLocator, ...]: ...
    async def purge(self, locator: PayloadLocator, fence: int) -> PurgeRecord: ...
    async def evidence(self, count: int, now: datetime) -> None: ...
