"""Authority-free typed intents and the server-owned execution scope."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _Trigger(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ChatTurnTrigger(_Trigger):
    kind: Literal["CHAT_TURN"] = "CHAT_TURN"
    turn_id: UUID


class _ReportTrigger(_Trigger):
    report_id: UUID
    base_version_id: UUID
    snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_key: str = Field(min_length=16, max_length=128)


class ReportRequestTrigger(_ReportTrigger):
    kind: Literal["REPORT_REQUEST"] = "REPORT_REQUEST"


class SummaryJobTrigger(_ReportTrigger):
    kind: Literal["SUMMARY_JOB"] = "SUMMARY_JOB"
    summary_id: UUID


type ExecutionTrigger = Annotated[
    ChatTurnTrigger | ReportRequestTrigger | SummaryJobTrigger, Field(discriminator="kind")
]
type NonChatTrigger = Annotated[
    ReportRequestTrigger | SummaryJobTrigger, Field(discriminator="kind")
]


class ExecutionScope(_Trigger):
    organization_id: UUID
    actor_membership_id: UUID
    orchestration_run_id: UUID
    trigger: ExecutionTrigger

    @property
    def identity(self) -> UUID:
        # Preserve existing chat run/handoff/checkpoint identities during additive rollout.
        return (
            self.trigger.turn_id
            if isinstance(self.trigger, ChatTurnTrigger)
            else self.orchestration_run_id
        )
