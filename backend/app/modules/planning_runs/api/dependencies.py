"""Typed Task 7 service dependencies from application state."""

from typing import Annotated, cast

from fastapi import Depends, Request

from app.modules.planning_runs.application.approval_service import ApprovalService
from app.modules.planning_runs.application.draft_service import PlanningDraftService
from app.modules.planning_runs.application.event_service import WorkflowEventService
from app.modules.planning_runs.application.proposal_service import ProposalService
from app.modules.planning_runs.application.run_service import PlanningRunService


def get_planning_run_service(request: Request) -> PlanningRunService:
    return cast(PlanningRunService, request.app.state.planning_run_service)


def get_proposal_service(request: Request) -> ProposalService:
    return cast(ProposalService, request.app.state.proposal_service)


def get_workflow_event_service(request: Request) -> WorkflowEventService:
    return cast(WorkflowEventService, request.app.state.workflow_event_service)


def get_approval_service(request: Request) -> ApprovalService:
    return cast(ApprovalService, request.app.state.approval_service)


PlanningRunServiceDependency = Annotated[PlanningRunService, Depends(get_planning_run_service)]
ProposalServiceDependency = Annotated[ProposalService, Depends(get_proposal_service)]
WorkflowEventServiceDependency = Annotated[
    WorkflowEventService, Depends(get_workflow_event_service)
]
ApprovalServiceDependency = Annotated[ApprovalService, Depends(get_approval_service)]


def get_planning_draft_service(request: Request) -> PlanningDraftService:
    return cast(PlanningDraftService, request.app.state.planning_draft_service)


PlanningDraftServiceDependency = Annotated[
    PlanningDraftService, Depends(get_planning_draft_service)
]
