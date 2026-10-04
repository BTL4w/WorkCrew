"""Typed contracts for permission-safe Work Intelligence."""

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from work_management_ai.runtime.contracts import JsonValue, RequestedHandoff


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkQuestionKind(StrEnum):
    PROGRESS = "PROGRESS"
    EVIDENCE = "EVIDENCE"
    RISK = "RISK"
    MY_TASKS = "MY_TASKS"
    NEXT_TASK = "NEXT_TASK"
    TASK_DETAIL = "TASK_DETAIL"
    PROJECT_DETAIL = "PROJECT_DETAIL"
    TASK_DEPENDENCIES = "TASK_DEPENDENCIES"
    ACCEPTANCE_CRITERIA = "ACCEPTANCE_CRITERIA"


class WorkIntelligenceInput(_StrictFrozenModel):
    question: str = Field(min_length=1, max_length=8_000)
    locale: Literal["vi", "en"]
    requested_kind: WorkQuestionKind | None
    entity_reference: str | None = Field(default=None, max_length=300)


class EvidenceItem(_StrictFrozenModel):
    evidence_id: str = Field(min_length=1, max_length=200)
    resource_type: Literal["PROJECT", "TASK", "DEPENDENCY", "ACCEPTANCE_CRITERION", "RISK_CONTEXT"]
    resource_id: UUID
    resource_version: int | None = Field(default=None, ge=1)
    fields: dict[str, JsonValue]
    observed_at: datetime


class EvidenceAssertion(_StrictFrozenModel):
    evidence_id: str = Field(min_length=1, max_length=200)
    field: str = Field(min_length=1, max_length=100)
    value: JsonValue


class GroundedClaim(_StrictFrozenModel):
    text: str = Field(min_length=1, max_length=20_000)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=16)
    assertions: tuple[EvidenceAssertion, ...] = Field(min_length=1, max_length=32)


class WorkIntelligenceOutput(_StrictFrozenModel):
    question_kind: WorkQuestionKind
    claims: tuple[GroundedClaim, ...] = Field(max_length=16)
    evidence: tuple[EvidenceItem, ...] = Field(max_length=64)
    needs_clarification: bool
    clarification_question: str | None = Field(default=None, max_length=4_000)

    @model_validator(mode="after")
    def clarification_is_explicit(self) -> "WorkIntelligenceOutput":
        if self.needs_clarification != (self.clarification_question is not None):
            raise ValueError("clarification flag and question must agree")
        return self


class WorkStepPlan(_StrictFrozenModel):
    question_kind: WorkQuestionKind
    skill_reference: Literal["answer_work_question@1"]
    tool_id: Literal["work.read_my_tasks", "work.read_resource", "risk.read"] | None
    tool_input: dict[str, JsonValue]
    requested_handoff: RequestedHandoff | None

    @model_validator(mode="after")
    def require_tool_or_handoff(self) -> "WorkStepPlan":
        if (self.tool_id is None) == (self.requested_handoff is None):
            raise ValueError("exactly one tool or requested handoff is required")
        return self


class GroundedAnswerDraft(_StrictFrozenModel):
    question_kind: WorkQuestionKind
    claims: tuple[GroundedClaim, ...] = Field(max_length=16)
    needs_clarification: bool
    clarification_question: str | None = Field(default=None, max_length=4_000)


class ReadToolEnvelope(_StrictFrozenModel):
    resolution: Literal["UNIQUE", "AMBIGUOUS", "NOT_FOUND"]
    evidence: tuple[EvidenceItem, ...] = Field(max_length=64)
    next_task_id: UUID | None = None
