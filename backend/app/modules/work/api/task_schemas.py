"""Strict public Task request and response schemas."""

from datetime import date, datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.progress.domain.completion import CriterionAttestation
from app.modules.progress.domain.daily_updates import SelectedEvidence
from app.modules.work.application.task_ports import TaskPage
from app.modules.work.domain.tasks import Task, TaskStatus
from app.modules.work.planning.assignment.application.assignment_service import (
    ExplicitAssignmentResult,
)


class TaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    project_week_id: UUID
    milestone_id: UUID | None = None
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    assignee_membership_id: UUID | None = None
    required_skill_labels: list[str] = Field(default_factory=list, max_length=20)
    estimated_effort_hours: int = Field(ge=1, le=10_000)
    due_date: date | None = None

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        if not (normalized := value.strip()):
            raise ValueError("title must not be blank")
        return normalized


class TaskUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    assignee_membership_id: UUID | None = None
    due_date: date | None = None
    milestone_id: UUID | None = None
    project_week_id: UUID | None = None
    required_skill_labels: list[str] | None = Field(default=None, max_length=20)
    estimated_effort_hours: int | None = Field(default=None, ge=1, le=10_000)


class CriterionAttestationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion_id: UUID
    version: int = Field(ge=1)
    confirmed: bool
    evidence_refs: list[SelectedEvidence] = Field(
        default_factory=lambda: list[SelectedEvidence](), max_length=20
    )

    def domain(self) -> CriterionAttestation:
        return CriterionAttestation(
            self.criterion_id, self.version, self.confirmed, tuple(self.evidence_refs)
        )


class TaskStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to_status: TaskStatus
    attestations: list[CriterionAttestationRequest] = Field(
        default_factory=lambda: list[CriterionAttestationRequest]()
    )


class ExplicitAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignee_membership_id: UUID
    expected_task_version: int = Field(ge=1)


class AssignmentWarningResponse(BaseModel):
    code: str


class AssigneeResponse(BaseModel):
    membership_id: UUID
    display_name: str


class TaskResponse(BaseModel):
    id: UUID
    project_id: UUID
    project_week_id: UUID | None
    milestone_id: UUID | None
    title: str
    description: str | None
    assignee: AssigneeResponse | None
    required_skill_labels: list[str]
    estimated_effort_hours: int | None
    status: TaskStatus
    due_date: date | None
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, task: Task) -> Self:
        return cls(
            id=task.id,
            project_id=task.project_id,
            project_week_id=task.project_week_id,
            milestone_id=task.milestone_id,
            title=task.title,
            description=task.description,
            assignee=(
                AssigneeResponse(
                    membership_id=task.assignee_membership_id,
                    display_name=task.assignee_display_name or "",
                )
                if task.assignee_membership_id is not None
                else None
            ),
            required_skill_labels=list(task.required_skill_labels),
            estimated_effort_hours=task.estimated_effort_hours,
            status=task.status,
            due_date=task.due_date,
            version=task.version,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )


class TaskPageResponse(BaseModel):
    items: list[TaskResponse]
    page: int
    page_size: int
    total: int

    @classmethod
    def from_domain(cls, result: TaskPage) -> Self:
        return cls(
            items=[TaskResponse.from_domain(task) for task in result.items],
            page=result.page,
            page_size=result.page_size,
            total=result.total,
        )


class ExplicitAssignmentResponse(BaseModel):
    task: TaskResponse
    warnings: tuple[AssignmentWarningResponse, ...]
    effective_capacity_hours: int
    workload_before_hours: int
    workload_after_hours: int

    @classmethod
    def from_domain(cls, result: ExplicitAssignmentResult) -> Self:
        return cls(
            task=TaskResponse.from_domain(result.task),
            warnings=tuple(
                AssignmentWarningResponse(code=warning.code) for warning in result.warnings
            ),
            effective_capacity_hours=result.effective_capacity_hours,
            workload_before_hours=result.workload_before_hours,
            workload_after_hours=result.workload_after_hours,
        )
