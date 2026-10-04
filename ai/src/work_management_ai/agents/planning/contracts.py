"""Typed Planning Specialist Agent contracts."""

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from work_management_ai.agents.risk.contracts import RiskReplanRequest
from work_management_ai.runtime.contracts import JsonValue, RequestedHandoff


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlanningOperation(StrEnum):
    CREATE = "CREATE"
    RESUME_INPUT = "RESUME_INPUT"
    REVISE = "REVISE"
    EXPLAIN = "EXPLAIN"


class PlanningAgentInput(_StrictFrozenModel):
    operation: PlanningOperation
    locale: Literal["vi", "en"]
    brief: str = Field(min_length=1, max_length=8_000)
    workflow_run_id: UUID | None = None
    proposal_id: UUID | None = None
    expected_proposal_version: int | None = Field(default=None, ge=1)
    manager_instruction: str | None = Field(default=None, max_length=8_000)
    risk_context: RiskReplanRequest | None = None

    @model_validator(mode="after")
    def operation_references_are_complete(self) -> "PlanningAgentInput":
        if self.risk_context is not None:
            if (
                self.operation is not PlanningOperation.REVISE
                or not self.manager_instruction
                or self.risk_context.risk_assessment_id is None
            ):
                raise ValueError(
                    "risk revision requires a verified assessment and Manager instruction"
                )
            if self.workflow_run_id is not None or self.proposal_id is not None:
                raise ValueError("risk revision cannot select an unrelated proposal")
            return self
        if self.operation is PlanningOperation.CREATE:
            return self
        if self.workflow_run_id is None:
            raise ValueError("existing workflow run is required")
        if self.operation is PlanningOperation.RESUME_INPUT and not self.manager_instruction:
            raise ValueError("manager input is required")
        if self.operation is PlanningOperation.REVISE and (
            self.proposal_id is None
            or self.expected_proposal_version is None
            or not self.manager_instruction
        ):
            raise ValueError("exact proposal version and revision instruction are required")
        if self.operation is PlanningOperation.EXPLAIN and self.proposal_id is None:
            raise ValueError("proposal reference is required")
        return self


class PlanningAgentOutput(_StrictFrozenModel):
    operation: PlanningOperation
    workflow_run_id: UUID
    workflow_status: str = Field(min_length=1, max_length=100)
    proposal_id: UUID | None
    proposal_version: int | None = Field(default=None, ge=1)
    approval_id: UUID | None
    awaiting: Literal["NONE", "MANAGER_INPUT", "MANAGER_DECISION"]
    public_summary: str = Field(min_length=1, max_length=20_000)


class PlanningStepPlan(_StrictFrozenModel):
    skill_reference: Literal["create_project_plan@1", "revise_project_plan@1"]
    tool_id: Literal["planning.manage_run"] | None
    tool_input: dict[str, JsonValue]
    requested_handoff: RequestedHandoff | None

    @model_validator(mode="after")
    def require_tool_or_handoff(self) -> "PlanningStepPlan":
        if (self.tool_id is None) == (self.requested_handoff is None):
            raise ValueError("exactly one tool or requested handoff is required")
        return self
