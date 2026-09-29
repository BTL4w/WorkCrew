"""Integration tests for PostgreSQLPlanningRunRepository and planning runs persistence."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import create_database_engine
from app.modules.identity.domain.auth import AuthenticatedActor, MembershipRole
from app.modules.organization.adapters import database_models as _org_models
from app.modules.planning_runs.adapters.repository import PostgreSQLPlanningRunRepository
from app.modules.planning_runs.domain.models import (
    Approval,
    ApprovalStatus,
    InvalidTransitionError,
    OutboxEvent,
    OutboxStatus,
    PlanningRunDomainError,
    Proposal,
    ProposalVersion,
    WorkflowCheckpoint,
    WorkflowEvent,
    WorkflowJob,
    WorkflowJobStatus,
    WorkflowRun,
    WorkflowRunStatus,
)
from app.modules.work.adapters import database_models as _work_models

_MODELS = (_org_models, _work_models)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


def make_actor(
    org_id: UUID,
    role: MembershipRole = MembershipRole.MANAGER,
    membership_id: UUID | None = None,
) -> AuthenticatedActor:
    return AuthenticatedActor(
        user_id=uuid4(),
        email="test@example.com",
        display_name="Test User",
        membership_id=membership_id or uuid4(),
        organization_id=org_id,
        organization_name="Test Org",
        role=role,
    )


@pytest.mark.asyncio
async def test_workflow_run_and_job_atomic_creation() -> None:
    engine = create_database_engine(Settings(environment="test"))
    org_id, user_id, member_id, project_id, run_id, job_id = (uuid4() for _ in range(6))
    actor = make_actor(org_id, membership_id=member_id)

    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(
                text("INSERT INTO organizations (id, slug, name) VALUES (:id, :slug, 'Org Test')"),
                {"id": org_id, "slug": f"org-test-{org_id.hex}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, email_normalized, email_display, display_name, password_hash) "
                    "VALUES (:id, :email, :email, 'User Test', 'hash')"
                ),
                {"id": user_id, "email": f"{user_id.hex}@example.test"},
            )
            await connection.execute(
                text(
                    "INSERT INTO memberships (id, organization_id, user_id, role) "
                    "VALUES (:id, :org, :user, 'MANAGER')"
                ),
                {"id": member_id, "org": org_id, "user": user_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO projects (id, organization_id, name, "
                    "created_by_membership_id, updated_by_membership_id) "
                    "VALUES (:id, :org, 'Project Test', :member, :member)"
                ),
                {"id": project_id, "org": org_id, "member": member_id},
            )

            await connection.execute(
                text("SELECT set_config('app.organization_id', :val, true)"),
                {"val": str(org_id)},
            )

            session = AsyncSession(bind=connection, expire_on_commit=False)
            repo = PostgreSQLPlanningRunRepository(session)

            run = WorkflowRun.create(
                id=run_id,
                organization_id=org_id,
                project_id=project_id,
                requested_by_membership_id=member_id,
                workflow_name="planning",
                workflow_version="v1.0",
                verifier_version="planning-verifier-v1",
                input_goal_text="Test goal text",
            )
            job = WorkflowJob(
                id=job_id,
                organization_id=org_id,
                workflow_run_id=run_id,
                job_type="planning.start",
                status=WorkflowJobStatus.QUEUED,
                payload={"goal": "Test goal text"},
            )

            await repo.create_workflow_run(run=run, job=job)

            fetched_run = await repo.get_workflow_run(actor=actor, run_id=run_id)
            assert fetched_run is not None
            assert fetched_run.id == run_id
            assert fetched_run.status == WorkflowRunStatus.QUEUED
            assert fetched_run.input_goal_text == "Test goal text"
            assert fetched_run.verifier_version == "planning-verifier-v1"

            now = datetime.now(UTC)
            lease_until = now + timedelta(minutes=5)
            claimed = await repo.claim_job(
                organization_id=org_id, worker_id="worker-1", now=now, lease_until=lease_until
            )
            assert claimed is not None
            assert claimed.id == job_id
            assert claimed.locked_by_worker_id == "worker-1"

            await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_proposal_versioning_and_approval_superseding() -> None:
    engine = create_database_engine(Settings(environment="test"))
    org_id, user_id, member_id, project_id, run_id, proposal_id, version_id, approval_id = (
        uuid4() for _ in range(8)
    )
    actor = make_actor(org_id, membership_id=member_id)

    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(
                text("INSERT INTO organizations (id, slug, name) VALUES (:id, :slug, 'Org Test')"),
                {"id": org_id, "slug": f"org-prop-{org_id.hex}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, email_normalized, email_display, display_name, password_hash) "
                    "VALUES (:id, :email, :email, 'User Prop', 'hash')"
                ),
                {"id": user_id, "email": f"{user_id.hex}@example.test"},
            )
            await connection.execute(
                text(
                    "INSERT INTO memberships (id, organization_id, user_id, role) "
                    "VALUES (:id, :org, :user, 'MANAGER')"
                ),
                {"id": member_id, "org": org_id, "user": user_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO projects (id, organization_id, name, "
                    "created_by_membership_id, updated_by_membership_id) "
                    "VALUES (:id, :org, 'Project Prop', :member, :member)"
                ),
                {"id": project_id, "org": org_id, "member": member_id},
            )

            await connection.execute(
                text("SELECT set_config('app.organization_id', :val, true)"),
                {"val": str(org_id)},
            )

            session = AsyncSession(bind=connection, expire_on_commit=False)
            repo = PostgreSQLPlanningRunRepository(session)

            run = WorkflowRun.create(
                id=run_id,
                organization_id=org_id,
                project_id=project_id,
                requested_by_membership_id=member_id,
                workflow_name="planning",
                workflow_version="v1.0",
                verifier_version="planning-verifier-v1",
                input_goal_text="Proposal goal",
            )
            await repo.create_workflow_run(run=run)

            proposal = Proposal.create(
                id=proposal_id,
                organization_id=org_id,
                workflow_run_id=run_id,
                current_version_number=1,
            )
            initial_ver = ProposalVersion(
                id=version_id,
                organization_id=org_id,
                proposal_id=proposal_id,
                version_number=1,
                created_by_membership_id=member_id,
                content={"tasks": [{"title": "Task 1"}]},
                assumptions=[{"text": "Assumption 1"}],
            )

            await repo.create_proposal(proposal=proposal, initial_version=initial_ver)

            await connection.execute(text("SET LOCAL ROLE app_runtime"))
            with pytest.raises(DBAPIError, match="approval must be inserted as PENDING"):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            "INSERT INTO approvals "
                            "(id, organization_id, proposal_id, proposal_version_number, status, "
                            "decided_by_membership_id, decided_at, version) VALUES "
                            "(:id, :org, :proposal, 1, 'APPROVED', :member, now(), 2)"
                        ),
                        {
                            "id": uuid4(),
                            "org": org_id,
                            "proposal": proposal_id,
                            "member": member_id,
                        },
                    )
            await connection.execute(text("RESET ROLE"))

            approval = Approval.create(
                id=approval_id,
                organization_id=org_id,
                proposal_id=proposal_id,
                proposal_version_number=1,
            )
            await repo.create_approval(approval=approval)

            await connection.execute(text("SET LOCAL ROLE app_runtime"))
            await connection.execute(
                text("SELECT set_config('app.membership_id', :member, true)"),
                {"member": str(uuid4())},
            )
            with pytest.raises(DBAPIError, match="authenticated membership"):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            "UPDATE approvals SET status = 'APPROVED', "
                            "decided_by_membership_id = :member, decided_at = now(), "
                            "version = 2 WHERE id = :id"
                        ),
                        {"member": member_id, "id": approval_id},
                    )
            await connection.execute(text("RESET ROLE"))

            ready_prop = proposal.mark_ready_for_decision(approval_id=approval_id)
            await repo.update_proposal(actor=actor, proposal=ready_prop)

            v2_id = uuid4()
            ver2 = ProposalVersion(
                id=v2_id,
                organization_id=org_id,
                proposal_id=proposal_id,
                version_number=2,
                created_by_membership_id=member_id,
                content={"tasks": [{"title": "Task 1 Updated"}]},
                assumptions=[],
            )
            edited_prop = ready_prop.edit()
            superseded_approval = approval.mark_superseded()
            employee = make_actor(
                org_id,
                role=MembershipRole.EMPLOYEE,
                membership_id=member_id,
            )
            with pytest.raises(PermissionError, match="Manager"):
                await repo.edit_proposal(
                    actor=employee,
                    proposal=edited_prop,
                    version=ver2,
                    superseded_approval=superseded_approval,
                )
            forged_version = ProposalVersion(
                id=ver2.id,
                organization_id=ver2.organization_id,
                proposal_id=ver2.proposal_id,
                version_number=ver2.version_number,
                created_by_membership_id=uuid4(),
                content=ver2.content,
                assumptions=ver2.assumptions,
            )
            with pytest.raises(PermissionError, match="authenticated actor"):
                await repo.edit_proposal(
                    actor=actor,
                    proposal=edited_prop,
                    version=forged_version,
                    superseded_approval=superseded_approval,
                )
            await repo.edit_proposal(
                actor=actor,
                proposal=edited_prop,
                version=ver2,
                superseded_approval=superseded_approval,
            )

            # Once proposal is edited (moved to DRAFT), trying to approve the old approval fails
            stale_decision = approval.decide_approve(decided_by=member_id)
            stale_proposal_decision = ready_prop.mark_approved()
            with pytest.raises(RuntimeError, match="proposal is not READY_FOR_DECISION"):
                await repo.decide_approval(
                    actor=actor,
                    approval=stale_decision,
                    proposal=stale_proposal_decision,
                )

            fetched_approval = await repo.get_approval(actor=actor, approval_id=approval_id)
            assert fetched_approval is not None
            assert fetched_approval.status == ApprovalStatus.SUPERSEDED
            assert fetched_approval.version == 2

            await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_checkpoint_upsert_idempotency_and_stale_conflict() -> None:
    engine = create_database_engine(Settings(environment="test"))
    org_id, user_id, member_id, project_id, run_id = (uuid4() for _ in range(5))

    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(
                text("INSERT INTO organizations (id, slug, name) VALUES (:id, :slug, 'Chk Org')"),
                {"id": org_id, "slug": f"chk-org-{org_id.hex}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, email_normalized, email_display, display_name, password_hash) "
                    "VALUES (:id, :email, :email, 'User Chk', 'hash')"
                ),
                {"id": user_id, "email": f"{user_id.hex}@example.test"},
            )
            await connection.execute(
                text(
                    "INSERT INTO memberships (id, organization_id, user_id, role) "
                    "VALUES (:id, :org, :user, 'MANAGER')"
                ),
                {"id": member_id, "org": org_id, "user": user_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO projects (id, organization_id, name, "
                    "created_by_membership_id, updated_by_membership_id) "
                    "VALUES (:id, :org, 'Chk Proj', :member, :member)"
                ),
                {"id": project_id, "org": org_id, "member": member_id},
            )
            await connection.execute(
                text("SELECT set_config('app.organization_id', :val, true)"),
                {"val": str(org_id)},
            )

            session = AsyncSession(bind=connection, expire_on_commit=False)
            repo = PostgreSQLPlanningRunRepository(session)

            run = WorkflowRun.create(
                id=run_id,
                organization_id=org_id,
                project_id=project_id,
                requested_by_membership_id=member_id,
                workflow_name="planning",
                workflow_version="v1.0",
                verifier_version="verifier-v1",
                input_goal_text="Goal",
            )
            await repo.create_workflow_run(run=run)

            chk1 = WorkflowCheckpoint(
                id=uuid4(),
                organization_id=org_id,
                workflow_run_id=run_id,
                node="node_a",
                sequence=1,
                state={"key": "val1"},
            )
            saved = await repo.save_checkpoint(checkpoint=chk1)
            assert saved.id == chk1.id

            # Idempotent match returns same checkpoint
            chk1_repeat = WorkflowCheckpoint(
                id=uuid4(),
                organization_id=org_id,
                workflow_run_id=run_id,
                node="node_a",
                sequence=1,
                state={"key": "val1"},
            )
            saved_repeat = await repo.save_checkpoint(checkpoint=chk1_repeat)
            assert saved_repeat == chk1_repeat

            # Differing state for same sequence raises InvalidTransitionError
            chk1_conflict = WorkflowCheckpoint(
                id=uuid4(),
                organization_id=org_id,
                workflow_run_id=run_id,
                node="node_a",
                sequence=1,
                state={"key": "val_conflicting"},
            )
            with pytest.raises(InvalidTransitionError, match="Checkpoint conflict"):
                await repo.save_checkpoint(checkpoint=chk1_conflict)

            await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_outbox_claim_publish_failure_and_idempotency() -> None:
    engine = create_database_engine(Settings(environment="test"))
    org_id, event_id_1 = uuid4(), uuid4()

    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(
                text(
                    "INSERT INTO organizations (id, slug, name) VALUES (:id, :slug, 'Outbox Org')"
                ),
                {"id": org_id, "slug": f"outbox-test-{org_id.hex}"},
            )
            await connection.execute(
                text("SELECT set_config('app.organization_id', :val, true)"),
                {"val": str(org_id)},
            )

            session = AsyncSession(bind=connection, expire_on_commit=False)
            repo = PostgreSQLPlanningRunRepository(session)

            outbox_1 = OutboxEvent(
                id=uuid4(),
                organization_id=org_id,
                event_id=event_id_1,
                event_type="test.event",
                aggregate_type="proposal",
                aggregate_id=uuid4(),
                payload={"data": 1},
                max_attempts=2,
            )
            await repo.enqueue_outbox_event(organization_id=org_id, event=outbox_1)

            # Re-enqueuing exact event is idempotent
            re_enqueued = await repo.enqueue_outbox_event(organization_id=org_id, event=outbox_1)
            assert re_enqueued.event_id == event_id_1

            # Conflicting event payload with same event_id raises error
            conflicting_outbox = OutboxEvent(
                id=uuid4(),
                organization_id=org_id,
                event_id=event_id_1,
                event_type="test.event",
                aggregate_type="proposal",
                aggregate_id=outbox_1.aggregate_id,
                payload={"data": 999},
            )
            with pytest.raises(PlanningRunDomainError, match="Conflict"):
                await repo.enqueue_outbox_event(organization_id=org_id, event=conflicting_outbox)

            now = datetime.now(UTC)
            lease_until = now + timedelta(seconds=30)
            claimed = await repo.claim_pending_outbox_events(
                organization_id=org_id,
                worker_id="w-1",
                limit=10,
                now=now,
                lease_until=lease_until,
            )
            assert len(claimed) == 1
            assert claimed[0].event_id == event_id_1
            assert claimed[0].status == OutboxStatus.DISPATCHING
            assert claimed[0].attempt_count == 1

            # Wrong worker cannot mark published
            with pytest.raises(PlanningRunDomainError, match="Outbox publish failed"):
                await repo.mark_outbox_event_published(
                    organization_id=org_id,
                    event_id=event_id_1,
                    worker_id="wrong-worker",
                    now=now,
                    published_at=now,
                )

            # Expired lease cannot record failure
            past_now = now + timedelta(seconds=60)
            with pytest.raises(PlanningRunDomainError, match="Outbox failure record failed"):
                await repo.record_outbox_event_failure(
                    organization_id=org_id,
                    event_id=event_id_1,
                    worker_id="w-1",
                    now=past_now,
                    error_code="ERR_TIMEOUT",
                    error_message="Gateway timeout",
                    next_available_at=now,
                )

            # Fail attempt 1 with correct worker and valid lease -> returns to PENDING
            await repo.record_outbox_event_failure(
                organization_id=org_id,
                event_id=event_id_1,
                worker_id="w-1",
                now=now,
                error_code="ERR_TIMEOUT",
                error_message="Gateway timeout",
                next_available_at=now - timedelta(seconds=1),
            )

            # Claim attempt 2
            claimed_2 = await repo.claim_pending_outbox_events(
                organization_id=org_id,
                worker_id="w-2",
                limit=10,
                now=now,
                lease_until=lease_until,
            )
            assert len(claimed_2) == 1
            assert claimed_2[0].attempt_count == 2

            # Mark published
            await repo.mark_outbox_event_published(
                organization_id=org_id,
                event_id=event_id_1,
                worker_id="w-2",
                now=now,
                published_at=now,
            )

            await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_append_event_always_computes_sequence() -> None:
    engine = create_database_engine(Settings(environment="test"))
    org_id, user_id, member_id, project_id, run_id = (uuid4() for _ in range(5))

    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(
                text("INSERT INTO organizations (id, slug, name) VALUES (:id, :slug, 'Seq Org')"),
                {"id": org_id, "slug": f"seq-org-{org_id.hex}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, email_normalized, email_display, display_name, password_hash) "
                    "VALUES (:id, :email, :email, 'User Seq', 'hash')"
                ),
                {"id": user_id, "email": f"{user_id.hex}@example.test"},
            )
            await connection.execute(
                text(
                    "INSERT INTO memberships (id, organization_id, user_id, role) "
                    "VALUES (:id, :org, :user, 'MANAGER')"
                ),
                {"id": member_id, "org": org_id, "user": user_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO projects (id, organization_id, name, "
                    "created_by_membership_id, updated_by_membership_id) "
                    "VALUES (:id, :org, 'Seq Proj', :member, :member)"
                ),
                {"id": project_id, "org": org_id, "member": member_id},
            )
            await connection.execute(
                text("SELECT set_config('app.organization_id', :val, true)"),
                {"val": str(org_id)},
            )

            session = AsyncSession(bind=connection, expire_on_commit=False)
            repo = PostgreSQLPlanningRunRepository(session)

            run = WorkflowRun.create(
                id=run_id,
                organization_id=org_id,
                project_id=project_id,
                requested_by_membership_id=member_id,
                workflow_name="planning",
                workflow_version="v1.0",
                verifier_version="verifier-v1",
                input_goal_text="Goal",
            )
            await repo.create_workflow_run(run=run)

            # Even if caller supplies sequence=999, repo computes sequence=1
            custom_event = WorkflowEvent(
                id=uuid4(),
                organization_id=org_id,
                workflow_run_id=run_id,
                sequence=999,
                event_type="test.event",
                public_payload={"step": 1},
            )
            appended = await repo.append_event(event=custom_event)
            assert appended.sequence == 1

            # Next event gets sequence=2
            custom_event_2 = WorkflowEvent(
                id=uuid4(),
                organization_id=org_id,
                workflow_run_id=run_id,
                sequence=500,
                event_type="test.event.2",
                public_payload={"step": 2},
            )
            appended_2 = await repo.append_event(event=custom_event_2)
            assert appended_2.sequence == 2

            await transaction.rollback()
    finally:
        await engine.dispose()
