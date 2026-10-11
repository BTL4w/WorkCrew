"""Unit tests for AI planning runs, proposal, and approval domain models."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from app.modules.planning_runs.adapters.database_models import WorkflowRunModel
from app.modules.planning_runs.domain.models import (
    Approval,
    ApprovalStatus,
    InvalidTransitionError,
    PlanningRunDomainError,
    Proposal,
    ProposalStatus,
    ProposalVersion,
    WorkflowRun,
    WorkflowRunStatus,
)


def test_workflow_run_initialization_and_status_transitions() -> None:
    run_id = uuid4()
    org_id = uuid4()
    actor_id = uuid4()
    project_id = uuid4()

    run = WorkflowRun.create(
        id=run_id,
        organization_id=org_id,
        project_id=project_id,
        requested_by_membership_id=actor_id,
        workflow_name="planning",
        workflow_version="v1.0",
        verifier_version="planning-verifier-v1",
        input_goal_text="Launch marketing campaign",
    )

    assert run.id == run_id
    assert run.organization_id == org_id
    assert run.status == WorkflowRunStatus.QUEUED
    assert run.version == 1
    assert run.verifier_version == "planning-verifier-v1"

    running = run.mark_running()
    assert running.status == WorkflowRunStatus.RUNNING
    assert running.version == 2

    needs_input = running.mark_needs_input()
    assert needs_input.status == WorkflowRunStatus.NEEDS_INPUT
    assert needs_input.version == 3

    running_again = needs_input.mark_running()
    assert running_again.status == WorkflowRunStatus.RUNNING
    assert running_again.version == 4

    waiting = running_again.mark_waiting_for_decision()
    assert waiting.status == WorkflowRunStatus.WAITING_FOR_DECISION
    assert waiting.version == 5

    completed = waiting.mark_completed()
    assert completed.status == WorkflowRunStatus.COMPLETED
    assert completed.version == 6

    with pytest.raises(InvalidTransitionError):
        completed.mark_running()


def test_new_project_planning_run_does_not_require_existing_project() -> None:
    run = WorkflowRun.create(
        organization_id=uuid4(),
        project_id=None,
        requested_by_membership_id=uuid4(),
        workflow_name="project_planning",
        workflow_version="1.0.0",
        verifier_version="1.0.0",
        input_goal_text="Plan a new customer conference",
    )

    assert run.project_id is None
    assert WorkflowRunModel.__table__.c.project_id.nullable is True


def test_workflow_run_failure_branch() -> None:
    run = WorkflowRun.create(
        organization_id=uuid4(),
        project_id=uuid4(),
        requested_by_membership_id=uuid4(),
        workflow_name="planning",
        workflow_version="v1.0",
        verifier_version="v1",
        input_goal_text="Test failure",
    ).mark_running()

    failed = run.mark_failed(error_message="Model timed out")
    assert failed.status == WorkflowRunStatus.FAILED
    assert failed.error_message == "Model timed out"

    with pytest.raises(InvalidTransitionError):
        failed.mark_running()


def test_workflow_run_requires_non_empty_verifier_version() -> None:
    with pytest.raises(PlanningRunDomainError, match="verifier_version"):
        WorkflowRun.create(
            organization_id=uuid4(),
            project_id=uuid4(),
            requested_by_membership_id=uuid4(),
            workflow_name="planning",
            workflow_version="v1.0",
            verifier_version="   ",
            input_goal_text="Goal",
        )


def test_proposal_lifecycle_and_version_increment() -> None:
    proposal_id = uuid4()
    org_id = uuid4()
    run_id = uuid4()
    approval_id = uuid4()

    proposal = Proposal.create(
        id=proposal_id,
        organization_id=org_id,
        workflow_run_id=run_id,
        current_version_number=1,
    )
    assert proposal.status == ProposalStatus.DRAFT
    assert proposal.approval_id is None
    assert proposal.current_version_number == 1

    validating = proposal.mark_validating()
    assert validating.status == ProposalStatus.VALIDATING

    ready = validating.mark_ready_for_decision(approval_id=approval_id)
    assert ready.status == ProposalStatus.READY_FOR_DECISION
    assert ready.approval_id == approval_id

    # Editing auto-increments version number and supersedes approval
    edited = ready.edit()
    assert edited.status == ProposalStatus.DRAFT
    assert edited.current_version_number == 2
    assert edited.superseded_approval_id == approval_id
    assert edited.approval_id is None

    new_approval_id = uuid4()
    ready_again = edited.mark_ready_for_decision(approval_id=new_approval_id)
    assert ready_again.approval_id == new_approval_id

    approved = ready_again.mark_approved()
    assert approved.status == ProposalStatus.APPROVED
    assert approved.approval_id == new_approval_id

    with pytest.raises(InvalidTransitionError):
        approved.edit()


def test_proposal_stale_transition() -> None:
    proposal = Proposal.create(
        organization_id=uuid4(),
        workflow_run_id=uuid4(),
    )
    ready = proposal.mark_ready_for_decision(
        approval_id=uuid4(),
    )
    stale = ready.mark_stale()
    assert stale.status == ProposalStatus.STALE

    # STALE is not decisionable
    with pytest.raises(InvalidTransitionError):
        stale.mark_rejected()

    with pytest.raises(InvalidTransitionError):
        stale.mark_approved()


def test_proposal_version_metadata_and_creator_type_validation() -> None:
    prop_ver = ProposalVersion(
        id=uuid4(),
        organization_id=uuid4(),
        proposal_id=uuid4(),
        version_number=1,
        created_by_membership_id=uuid4(),
        content={"goal": "Launch"},
        assumptions=[{"text": "Budget available"}],
        workflow_version="planning-v1",
        prompt_version="prompt-v2",
        schema_version="schema-v1",
        model_reference="gpt-4o",
        verifier_version="verifier-v1",
        creator_type="AI_SYSTEM",
    )
    assert prop_ver.creator_type == "AI_SYSTEM"
    expected_validation: dict[str, Any] = {
        "status": "UNKNOWN",
        "is_valid": None,
        "errors": [],
        "warnings": [],
    }
    assert prop_ver.validation_result == expected_validation

    with pytest.raises(PlanningRunDomainError, match="creator_type"):
        ProposalVersion(
            id=uuid4(),
            organization_id=uuid4(),
            proposal_id=uuid4(),
            version_number=1,
            created_by_membership_id=uuid4(),
            content={},
            assumptions=[],
            creator_type="INVALID_CREATOR",
        )


def test_approval_versioning_and_concurrency() -> None:
    approval_id = uuid4()
    org_id = uuid4()
    proposal_id = uuid4()
    decided_by = uuid4()

    approval = Approval.create(
        id=approval_id,
        organization_id=org_id,
        proposal_id=proposal_id,
        proposal_version_number=1,
    )
    assert approval.status == ApprovalStatus.PENDING
    assert approval.version == 1

    decided_at = datetime.now(UTC)
    approved = approval.decide_approve(
        decided_by=decided_by, decision_reason="Looks good", decided_at=decided_at
    )
    assert approved.status == ApprovalStatus.APPROVED
    assert approved.version == 2
    assert approved.decided_by_membership_id == decided_by

    with pytest.raises(InvalidTransitionError):
        approved.decide_reject(
            decided_by=decided_by, decision_reason="Change mind", decided_at=decided_at
        )


def test_available_draft_can_resume_without_approving_business_writes() -> None:
    from dataclasses import replace
    from uuid import uuid4

    from app.modules.planning_runs.domain.models import WorkflowRun, WorkflowRunStatus

    run = WorkflowRun.create(
        organization_id=uuid4(),
        project_id=None,
        requested_by_membership_id=uuid4(),
        workflow_name="project_planning",
        workflow_version="3.0.0",
        verifier_version="1.0.0",
        input_goal_text="Course",
    )
    draft = replace(run, status=WorkflowRunStatus.DRAFT_AVAILABLE)
    assert draft.mark_running().status is WorkflowRunStatus.RUNNING
    assert draft.mark_running().mark_draft_available().status is WorkflowRunStatus.DRAFT_AVAILABLE
