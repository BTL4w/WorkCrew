"""Strict public schemas for asynchronous planning runs and proposals."""

from datetime import date, datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.planning_runs.application.approval_ports import (
    ApprovalDecision,
    ApprovalDecisionResult,
)
from app.modules.planning_runs.application.ports import WorkflowRunSnapshot
from app.modules.planning_runs.domain.models import Proposal, ProposalVersion, WorkflowRun


class PlanningRunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=8000)
    locale: Literal["vi", "en"]

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("message must not be blank")
        return normalized


class ManagerMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=8000)

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("message must not be blank")
        return normalized


class ProposalEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: dict[str, object]


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ApprovalDecision
    reason: str | None = Field(default=None, max_length=1000)


class ApprovalStatusResponse(BaseModel):
    id: UUID
    status: str


class DecidedProposalResponse(BaseModel):
    id: UUID
    version: int
    status: str


class CreatedBusinessIdsResponse(BaseModel):
    project_id: UUID | None
    goal_id: UUID | None
    milestone_ids: list[UUID]
    task_ids: list[UUID]
    dependency_ids: list[UUID]
    acceptance_criterion_ids: list[UUID]


class ApprovalDecisionResponse(BaseModel):
    approval: ApprovalStatusResponse
    proposal: DecidedProposalResponse
    created: CreatedBusinessIdsResponse
    workflow_run_id: UUID
    finalization_job_id: UUID

    @classmethod
    def from_result(cls, result: ApprovalDecisionResult) -> Self:
        return cls(
            approval=ApprovalStatusResponse(
                id=result.approval_id,
                status=result.approval_status.value,
            ),
            proposal=DecidedProposalResponse(
                id=result.proposal_id,
                version=result.proposal_version,
                status=result.proposal_status.value,
            ),
            created=CreatedBusinessIdsResponse(
                project_id=result.created.project_id,
                goal_id=result.created.goal_id,
                milestone_ids=list(result.created.milestone_ids),
                task_ids=list(result.created.task_ids),
                dependency_ids=list(result.created.dependency_ids),
                acceptance_criterion_ids=list(result.created.acceptance_criterion_ids),
            ),
            workflow_run_id=result.workflow_run_id,
            finalization_job_id=result.finalization_job_id,
        )


class WorkflowRunReferenceResponse(BaseModel):
    run_id: UUID
    status: str
    version: int

    @classmethod
    def from_domain(cls, run: WorkflowRun) -> Self:
        return cls(run_id=run.id, status=run.status.value, version=run.version)


class WorkflowProposalSnapshotResponse(BaseModel):
    proposal_id: UUID
    approval_id: UUID | None
    status: str
    version: int
    validation_result: dict[str, object]
    content: dict[str, object]
    change_summary: str | None
    field_provenance: dict[str, object]
    creator_type: str
    previous_version: "PreviousProposalVersionResponse | None"


class PreviousProposalVersionResponse(BaseModel):
    version: int
    content: dict[str, object]
    field_provenance: dict[str, object]
    creator_type: str


class WorkflowTimelineItemResponse(BaseModel):
    sequence: int
    event_type: str
    payload: dict[str, object]
    occurred_at: datetime


def _timeline_items() -> list[WorkflowTimelineItemResponse]:
    return []


class WorkflowRunResponse(BaseModel):
    id: UUID
    project_id: UUID | None
    status: str
    workflow_name: str
    workflow_version: str
    verifier_version: str
    input_goal_text: str
    version: int
    created_at: datetime
    updated_at: datetime
    current_stage: str | None = None
    current_proposal: WorkflowProposalSnapshotResponse | None = None
    public_timeline: list[WorkflowTimelineItemResponse] = Field(default_factory=_timeline_items)
    allowed_actions: list[str] = Field(default_factory=list)

    @classmethod
    def from_domain(cls, run: WorkflowRun) -> Self:
        return cls(
            id=run.id,
            project_id=run.project_id,
            status=run.status.value,
            workflow_name=run.workflow_name,
            workflow_version=run.workflow_version,
            verifier_version=run.verifier_version,
            input_goal_text=run.input_goal_text,
            version=run.version,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )

    @classmethod
    def from_snapshot(cls, snapshot: WorkflowRunSnapshot) -> Self:
        response = cls.from_domain(snapshot.run)
        response.current_stage = (
            snapshot.checkpoint.node
            if snapshot.checkpoint is not None
            else snapshot.run.status.value
        )
        if snapshot.proposal is not None and snapshot.proposal_version is not None:
            response.current_proposal = WorkflowProposalSnapshotResponse(
                proposal_id=snapshot.proposal.id,
                approval_id=snapshot.proposal.approval_id,
                status=snapshot.proposal.status.value,
                version=snapshot.proposal_version.version_number,
                validation_result=snapshot.proposal_version.validation_result,
                content=snapshot.proposal_version.content,
                change_summary=snapshot.proposal_version.change_summary,
                field_provenance=snapshot.proposal_version.field_provenance,
                creator_type=snapshot.proposal_version.creator_type,
                previous_version=(
                    PreviousProposalVersionResponse(
                        version=snapshot.previous_proposal_version.version_number,
                        content=snapshot.previous_proposal_version.content,
                        field_provenance=snapshot.previous_proposal_version.field_provenance,
                        creator_type=snapshot.previous_proposal_version.creator_type,
                    )
                    if snapshot.previous_proposal_version is not None
                    else None
                ),
            )
        response.public_timeline = [
            WorkflowTimelineItemResponse(
                sequence=event.sequence,
                event_type=event.event_type,
                payload=event.public_payload,
                occurred_at=event.created_at,
            )
            for event in snapshot.events
        ]
        actions: list[str] = []
        if snapshot.run.status.value == "NEEDS_INPUT":
            actions.append("MESSAGE")
        if snapshot.proposal is not None and not snapshot.proposal.status.is_terminal:
            actions.append("EDIT_PROPOSAL")
        if (
            snapshot.proposal is not None
            and snapshot.proposal.status.value == "READY_FOR_DECISION"
            and snapshot.proposal.approval_id is not None
        ):
            actions.append("DECIDE_APPROVAL")
        response.allowed_actions = actions
        return response


class WorkflowRunListResponse(BaseModel):
    items: list[WorkflowRunResponse]


class ProposalReferenceResponse(BaseModel):
    proposal_id: UUID
    workflow_run_id: UUID
    status: str
    version: int
    content: dict[str, object]

    @classmethod
    def from_domain(cls, proposal: Proposal, version: ProposalVersion) -> Self:
        return cls(
            proposal_id=proposal.id,
            workflow_run_id=proposal.workflow_run_id,
            status=proposal.status.value,
            version=version.version_number,
            content=version.content,
        )


class ProposalVersionResponse(BaseModel):
    proposal_id: UUID
    workflow_run_id: UUID
    version: int
    current_version: int
    content: dict[str, object]
    creator_type: str

    @classmethod
    def from_domain(cls, proposal: Proposal, version: ProposalVersion) -> Self:
        return cls(
            proposal_id=proposal.id,
            workflow_run_id=proposal.workflow_run_id,
            version=version.version_number,
            current_version=proposal.current_version_number,
            content=version.content,
            creator_type=version.creator_type,
        )


class PlanningDraftResponse(BaseModel):
    draft_id: UUID
    workflow_run_id: UUID
    draft_version: int
    current_version: int
    can_prepare: bool
    suggested_anchor_date: date | None
    fingerprint: str
    content: dict[str, object]
    question: dict[str, object] | None
    validation: dict[str, object]
    versions: dict[str, str]
    change_summary: str | None
