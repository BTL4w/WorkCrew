"""Relative planning content; application-issued identity is kept separate."""

from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictPlanningModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlanningFieldStatus(StrEnum):
    USER_STATED = "USER_STATED"
    AUTHORIZED_SOURCE = "AUTHORIZED_SOURCE"
    AI_ASSUMPTION = "AI_ASSUMPTION"
    UNKNOWN = "UNKNOWN"
    CONFLICTING = "CONFLICTING"


class PlanningField(StrictPlanningModel):
    value: str | int | list[str] | None = None
    status: PlanningFieldStatus = PlanningFieldStatus.UNKNOWN
    source_refs: tuple[str, ...] = ()
    assumption_reason: str | None = None

    @model_validator(mode="after")
    def require_provenance(self) -> Self:
        if self.status is PlanningFieldStatus.AI_ASSUMPTION and not self.assumption_reason:
            raise ValueError("assumption requires a visible reason")
        if (
            self.status in {PlanningFieldStatus.USER_STATED, PlanningFieldStatus.AUTHORIZED_SOURCE}
            and not self.source_refs
        ):
            raise ValueError("stated facts require source references")
        if self.status is PlanningFieldStatus.UNKNOWN and self.value is not None:
            raise ValueError("unknown fields must not contain invented values")
        return self


class PlanningBudget(StrictPlanningModel):
    amount: int = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    status: PlanningFieldStatus
    source_refs: tuple[str, ...] = ()
    assumption_reason: str | None = None

    @model_validator(mode="after")
    def require_provenance(self) -> Self:
        PlanningField(
            value=self.amount,
            status=self.status,
            source_refs=self.source_refs,
            assumption_reason=self.assumption_reason,
        )
        return self


class PlanningBrief(StrictPlanningModel):
    objective: PlanningField
    audience: PlanningField = Field(default_factory=PlanningField)
    expected_outcomes: tuple[PlanningField, ...] = ()
    scope_in: tuple[PlanningField, ...] = ()
    scope_out: tuple[PlanningField, ...] = ()
    timing: PlanningField = Field(default_factory=PlanningField)
    budget: PlanningBudget | None = None
    constraints: tuple[PlanningField, ...] = ()


class PlanningQuestion(StrictPlanningModel):
    id: str = Field(min_length=1, max_length=128)
    purpose: Literal["GOAL", "DECISIVE", "FOLLOW_UP"]
    text: str = Field(min_length=1, max_length=4000)
    decision_impact: str | None = None
    blocking: bool
    source_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def check_blocking(self) -> Self:
        if self.blocking != (self.purpose != "FOLLOW_UP"):
            raise ValueError("only goal/decisive questions are blocking")
        if self.purpose == "DECISIVE" and not self.decision_impact:
            raise ValueError("decisive question requires structural impact")
        return self


class DraftPhase(StrictPlanningModel):
    ref: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    start_day: int = Field(ge=0)
    end_day: int = Field(ge=1)


class DraftTask(StrictPlanningModel):
    ref: str = Field(min_length=1, max_length=128)
    phase_ref: str
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    start_day: int = Field(ge=0)
    end_day: int = Field(ge=1)
    duration_days: int = Field(ge=1, le=3650)
    duration_source: Literal["USER_STATED", "AUTHORIZED_SOURCE", "AI_ASSUMPTION"]
    duration_source_refs: tuple[str, ...] = ()
    estimated_effort_hours: int = Field(default=8, ge=1, le=10000)
    required_skill_labels: tuple[str, ...] = Field(default=(), max_length=20)
    acceptance_criteria: tuple[str, ...] = Field(default=(), max_length=20)


class DraftDependency(StrictPlanningModel):
    predecessor_ref: str
    successor_ref: str


class PlanningDraft(StrictPlanningModel):
    schema_version: Literal["planning-draft.v1"] = "planning-draft.v1"
    brief: PlanningBrief
    duration_days: int = Field(ge=1, le=3650)
    phases: tuple[DraftPhase, ...] = Field(min_length=1, max_length=20)
    tasks: tuple[DraftTask, ...] = Field(min_length=1, max_length=100)
    dependencies: tuple[DraftDependency, ...] = Field(default=(), max_length=200)
    assumptions: tuple[PlanningField, ...] = ()
    open_questions: tuple[PlanningQuestion, ...] = Field(default=(), max_length=1)


class PlanningDecision(StrictPlanningModel):
    kind: Literal["ASK_GOAL", "ASK_DECISIVE", "DRAFT"]
    question: PlanningQuestion | None = None
    draft: PlanningDraft | None = None

    @model_validator(mode="after")
    def validate_branch(self) -> Self:
        if self.kind == "DRAFT":
            if self.draft is None or (self.question is not None and self.question.blocking):
                raise ValueError(
                    "draft decision requires content and optional nonblocking question"
                )
            if any(question.blocking for question in self.draft.open_questions):
                raise ValueError("draft cannot contain blocking questions")
            if (
                self.question is not None
                and self.draft.open_questions
                and self.draft.open_questions != (self.question,)
            ):
                raise ValueError("draft decision can ask only one follow-up")
        elif (
            self.draft is not None
            or self.question is None
            or self.question.purpose != self.kind.removeprefix("ASK_")
        ):
            raise ValueError("ask decision requires exactly one matching question and no draft")
        return self


class DraftBinding(StrictPlanningModel):
    draft_id: UUID
    draft_version: int = Field(ge=1)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class PlanningChangeAssessment(StrictPlanningModel):
    affected_field_paths: tuple[str, ...] = ()
    affected_phase_refs: tuple[str, ...] = ()
    affected_task_refs: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    reason: str = ""


class PlanningGroundingResult(StrictPlanningModel):
    passed: bool
    unsupported_claim_paths: tuple[str, ...] = ()
    constraint_violation_paths: tuple[str, ...] = ()
    safe_reasons: tuple[str, ...] = ()


class ProgressivePlanningCandidate(StrictPlanningModel):
    decision: PlanningDecision
    change_assessment: PlanningChangeAssessment | None = None
