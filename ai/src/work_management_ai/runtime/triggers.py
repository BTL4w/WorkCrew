"""Authority-free typed intents and the server-owned execution scope."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)


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
    mode: Literal["DRAFT", "VERIFY_EDIT"] = "DRAFT"
    edited_version_id: UUID | None = None

    @model_serializer(mode="wrap")
    def legacy_wire(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data: dict[str, object] = handler(self)
        if self.mode == "DRAFT":
            data.pop("mode", None)
            data.pop("edited_version_id", None)
        return data

    @model_validator(mode="after")
    def edit_shape(self) -> Self:
        if (self.mode == "VERIFY_EDIT") != (self.edited_version_id is not None):
            raise ValueError("edit verification trigger shape")
        if self.edited_version_id is not None and self.edited_version_id != self.base_version_id:
            raise ValueError("edit version must equal base version")
        return self


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
