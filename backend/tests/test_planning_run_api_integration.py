"""ASGI integration tests for Task 7 planning-run and proposal APIs."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pwdlib import PasswordHash
from sqlalchemy import text

from app.core.config import Settings
from app.core.database import create_database_engine, create_session_factory
from app.main import create_app
from app.modules.identity.adapters.auth_repository import SqlAlchemyAuthTransactionFactory
from app.modules.identity.adapters.current_actor import CurrentActorResolver
from app.modules.identity.api.dependencies import get_authenticated_actor
from app.modules.identity.application.current_actor_service import CurrentActorService
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.planning_runs.adapters.ai_runtime import build_planning_job_handlers
from app.modules.planning_runs.adapters.transaction import (
    PostgreSQLPlanningRunTransactionFactory,
)
from app.modules.planning_runs.api.schemas import WorkflowRunResponse
from app.modules.planning_runs.application.job_service import JobService
from app.modules.planning_runs.application.ports import (
    ProposalMutationResult,
    WorkflowRunMutationResult,
    WorkflowRunSnapshot,
)
from app.modules.planning_runs.domain.models import (
    PlanningRunForbiddenError,
    PlanningRunNotFoundError,
    Proposal,
    ProposalVersion,
    ResourceVersionMismatchError,
    WorkflowRun,
    WorkflowRunStatus,
)


def actor(role: MembershipRole = MembershipRole.MANAGER) -> AuthenticatedActor:
    return AuthenticatedActor(
        user_id=uuid4(),
        email="manager@example.test",
        display_name="Manager",
        membership_id=uuid4(),
        organization_id=uuid4(),
        organization_name="Tenant",
        role=role,
    )


def run_for(current_actor: AuthenticatedActor) -> WorkflowRun:
    now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    return WorkflowRun(
        id=uuid4(),
        organization_id=current_actor.organization_id,
        project_id=None,
        requested_by_membership_id=current_actor.membership_id,
        status=WorkflowRunStatus.QUEUED,
        workflow_name="project_planning",
        workflow_version="1.0.0",
        verifier_version="1.0.0",
        input_goal_text="Plan a conference",
        version=1,
        created_at=now,
        updated_at=now,
    )


def test_snapshot_exposes_current_approval_provenance_and_previous_version() -> None:
    current_actor = actor()
    run = replace(run_for(current_actor), status=WorkflowRunStatus.WAITING_FOR_DECISION)
    approval_id = uuid4()
    proposal = Proposal.create(
        organization_id=current_actor.organization_id,
        workflow_run_id=run.id,
        current_version_number=2,
    ).mark_ready_for_decision(approval_id)
    previous = ProposalVersion(
        id=uuid4(),
        organization_id=current_actor.organization_id,
        proposal_id=proposal.id,
        version_number=1,
        created_by_membership_id=current_actor.membership_id,
        content={"project": {"title": "AI draft"}},
        assumptions=[],
        field_provenance={"default": "AI_PROPOSED"},
        creator_type="AI_SYSTEM",
    )
    current = ProposalVersion(
        id=uuid4(),
        organization_id=current_actor.organization_id,
        proposal_id=proposal.id,
        version_number=2,
        created_by_membership_id=current_actor.membership_id,
        content={"project": {"title": "Manager draft"}},
        assumptions=[],
        change_summary="Manager edited proposal",
        field_provenance={"default": "MANAGER_EDITED"},
        creator_type="HUMAN_MANAGER",
    )

    response = WorkflowRunResponse.from_snapshot(
        WorkflowRunSnapshot(
            run=run,
            checkpoint=None,
            proposal=proposal,
            proposal_version=current,
            previous_proposal_version=previous,
            events=(),
        )
    ).model_dump(mode="json")

    assert response["current_proposal"] == {
        "proposal_id": str(proposal.id),
        "approval_id": str(approval_id),
        "status": "READY_FOR_DECISION",
        "version": 2,
        "validation_result": {
            "status": "UNKNOWN",
            "is_valid": None,
            "errors": [],
            "warnings": [],
        },
        "content": {"project": {"title": "Manager draft"}},
        "change_summary": "Manager edited proposal",
        "field_provenance": {"default": "MANAGER_EDITED"},
        "creator_type": "HUMAN_MANAGER",
        "previous_version": {
            "version": 1,
            "content": {"project": {"title": "AI draft"}},
            "field_provenance": {"default": "AI_PROPOSED"},
            "creator_type": "AI_SYSTEM",
        },
    }
    assert response["allowed_actions"] == ["EDIT_PROPOSAL", "DECIDE_APPROVAL"]


class StubRunService:
    def __init__(self, current_actor: AuthenticatedActor) -> None:
        self.run = run_for(current_actor)
        self.last_create: dict[str, Any] | None = None
        self.last_message: dict[str, Any] | None = None
        self.error: Exception | None = None

    async def create_planning_run(self, **values: Any) -> WorkflowRunMutationResult:
        if self.error:
            raise self.error
        self.last_create = values
        return WorkflowRunMutationResult(run=self.run, replayed=False)

    async def list_workflow_runs(self, **_: object) -> tuple[WorkflowRun, ...]:
        if self.error:
            raise self.error
        return (self.run,)

    async def get_workflow_run(self, **_: object) -> WorkflowRun:
        if self.error:
            raise self.error
        return self.run

    async def get_workflow_run_snapshot(self, **_: object) -> WorkflowRunSnapshot:
        if self.error:
            raise self.error
        return WorkflowRunSnapshot(
            run=self.run,
            checkpoint=None,
            proposal=None,
            proposal_version=None,
            events=(),
        )

    async def post_manager_message(self, **values: Any) -> WorkflowRunMutationResult:
        if self.error:
            raise self.error
        self.last_message = values
        return WorkflowRunMutationResult(
            run=replace(self.run, status=WorkflowRunStatus.RUNNING, version=2),
            replayed=False,
        )


class StubProposalService:
    def __init__(self, current_actor: AuthenticatedActor) -> None:
        self.proposal = Proposal.create(
            organization_id=current_actor.organization_id,
            workflow_run_id=uuid4(),
            current_version_number=4,
        )
        self.error: Exception | None = None
        self.last_edit: dict[str, Any] | None = None
        self.last_get: dict[str, Any] | None = None

    async def get_proposal_version(self, **values: Any) -> tuple[Proposal, ProposalVersion]:
        if self.error:
            raise self.error
        self.last_get = values
        version = ProposalVersion(
            id=uuid4(),
            organization_id=self.proposal.organization_id,
            proposal_id=self.proposal.id,
            version_number=values["version_number"],
            created_by_membership_id=values["actor"].membership_id,
            content={"project": {"title": "Immutable version"}},
            assumptions=[],
            creator_type="AI_SYSTEM",
        )
        return self.proposal, version

    async def edit_proposal(self, **values: Any) -> ProposalMutationResult:
        if self.error:
            raise self.error
        self.last_edit = values
        edited = self.proposal.edit()
        version = ProposalVersion(
            id=uuid4(),
            organization_id=edited.organization_id,
            proposal_id=edited.id,
            version_number=5,
            created_by_membership_id=values["actor"].membership_id,
            content=values["content"],
            assumptions=[],
            creator_type="HUMAN_MANAGER",
        )
        return ProposalMutationResult(proposal=edited, version=version, replayed=False)


def app_with(
    current_actor: AuthenticatedActor,
    run_service: StubRunService,
    proposal_service: StubProposalService,
) -> FastAPI:
    app = create_app(
        Settings(environment="test"),
        planning_run_service=run_service,  # type: ignore[arg-type]
        proposal_service=proposal_service,  # type: ignore[arg-type]
    )
    app.dependency_overrides[get_authenticated_actor] = lambda: current_actor
    return app


@pytest.mark.asyncio
async def test_create_list_snapshot_message_and_proposal_edit_contracts() -> None:
    current_actor = actor()
    run_service = StubRunService(current_actor)
    proposal_service = StubProposalService(current_actor)
    transport = ASGITransport(app=app_with(current_actor, run_service, proposal_service))
    proposal_payload: dict[str, object] = {
        "project": {
            "title": "Conference",
            "description": None,
            "start_date": None,
            "due_date": None,
        },
        "goal": {
            "title": "Conference",
            "description": None,
            "expected_outcomes": [],
            "target_date": None,
        },
        "milestones": [],
        "tasks": [],
        "dependencies": [],
        "assumptions": [],
    }

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post(
            "/api/v1/ai/planning-runs",
            json={"message": "Plan a conference", "locale": "en"},
            headers={"Idempotency-Key": "planning-create-key"},
        )
        listed = await client.get("/api/v1/workflow-runs")
        snapshot = await client.get(f"/api/v1/workflow-runs/{run_service.run.id}")
        proposal_version = await client.get(
            f"/api/v1/proposals/{proposal_service.proposal.id}/versions/1"
        )
        message = await client.post(
            f"/api/v1/workflow-runs/{run_service.run.id}/messages",
            json={"message": "Budget is 50,000 USD"},
            headers={"Idempotency-Key": "planning-message-key"},
        )
        edited = await client.patch(
            f"/api/v1/proposals/{proposal_service.proposal.id}",
            json={"content": proposal_payload},
            headers={"Idempotency-Key": "proposal-edit-key", "If-Match": '"4"'},
        )

    assert created.status_code == 202
    assert created.headers["location"] == f"/api/v1/workflow-runs/{run_service.run.id}"
    assert created.json()["run_id"] == str(run_service.run.id)
    assert listed.status_code == 200 and len(listed.json()["items"]) == 1
    assert snapshot.status_code == 200 and snapshot.json()["status"] == "QUEUED"
    assert proposal_version.status_code == 200
    assert proposal_version.json() == {
        "proposal_id": str(proposal_service.proposal.id),
        "workflow_run_id": str(proposal_service.proposal.workflow_run_id),
        "version": 1,
        "current_version": 4,
        "content": {"project": {"title": "Immutable version"}},
        "creator_type": "AI_SYSTEM",
    }
    assert proposal_service.last_get is not None
    assert proposal_service.last_get["version_number"] == 1
    assert message.status_code == 202
    assert edited.status_code == 202
    assert edited.headers["etag"] == '"5"'
    assert proposal_service.last_edit is not None
    assert proposal_service.last_edit["expected_version"] == 4


@pytest.mark.asyncio
async def test_missing_and_stale_if_match_use_structured_errors() -> None:
    current_actor = actor()
    run_service = StubRunService(current_actor)
    proposal_service = StubProposalService(current_actor)
    proposal_service.error = ResourceVersionMismatchError(5)
    transport = ASGITransport(app=app_with(current_actor, run_service, proposal_service))

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing = await client.patch(
            f"/api/v1/proposals/{proposal_service.proposal.id}",
            json={"content": {}},
            headers={"Idempotency-Key": "proposal-edit-key"},
        )
        stale = await client.patch(
            f"/api/v1/proposals/{proposal_service.proposal.id}",
            json={"content": {}},
            headers={"Idempotency-Key": "proposal-edit-key", "If-Match": '"4"'},
        )

    assert missing.status_code == 428
    assert missing.json()["error"]["code"] == "PRECONDITION_REQUIRED"
    assert stale.status_code == 412
    assert stale.json()["error"]["code"] == "RESOURCE_VERSION_MISMATCH"
    assert stale.json()["error"]["details"] == {"current_version": 5}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status_code"),
    [(PlanningRunForbiddenError(), 403), (PlanningRunNotFoundError(), 404)],
)
async def test_authorization_and_non_disclosure_errors_are_structured(
    error: Exception, status_code: int
) -> None:
    current_actor = actor(MembershipRole.EMPLOYEE)
    run_service = StubRunService(current_actor)
    run_service.error = error
    proposal_service = StubProposalService(current_actor)
    transport = ASGITransport(app=app_with(current_actor, run_service, proposal_service))

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/v1/workflow-runs")

    assert response.status_code == status_code
    assert response.json()["error"]["code"] in {"FORBIDDEN", "RESOURCE_NOT_FOUND"}
    assert "traceback" not in response.text.casefold()


@pytest.mark.asyncio
async def test_unexpected_runtime_failure_is_normalized_without_provider_details() -> None:
    current_actor = actor()
    run_service = StubRunService(current_actor)
    run_service.error = RuntimeError("raw provider secret and prompt")
    proposal_service = StubProposalService(current_actor)
    transport = ASGITransport(
        app=app_with(current_actor, run_service, proposal_service),
        raise_app_exceptions=False,
    )

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/v1/workflow-runs")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "provider" not in response.text.casefold()
    assert "prompt" not in response.text.casefold()
    assert "secret" not in response.text.casefold()


def test_openapi_exposes_only_task7_planning_mutation_and_stream_contracts() -> None:
    current_actor = actor()
    schema = app_with(
        current_actor,
        StubRunService(current_actor),
        StubProposalService(current_actor),
    ).openapi()
    expected_methods = {
        "/api/v1/ai/planning-runs": {"post"},
        "/api/v1/workflow-runs": {"get"},
        "/api/v1/workflow-runs/{run_id}": {"get"},
        "/api/v1/workflow-runs/{run_id}/messages": {"post"},
        "/api/v1/proposals/{proposal_id}": {"patch"},
        "/api/v1/proposals/{proposal_id}/versions/{version_number}": {"get"},
        "/api/v1/workflow-runs/{run_id}/events": {"get"},
    }

    for path, methods in expected_methods.items():
        assert methods <= set(schema["paths"][path])
    assert "/api/v1/proposals/{proposal_id}/decision" not in schema["paths"]
    create_headers = {
        parameter["name"]
        for parameter in schema["paths"]["/api/v1/ai/planning-runs"]["post"]["parameters"]
        if parameter["in"] == "header"
    }
    edit_headers = {
        parameter["name"]
        for parameter in schema["paths"]["/api/v1/proposals/{proposal_id}"]["patch"]["parameters"]
        if parameter["in"] == "header"
    }
    assert create_headers == {"Idempotency-Key"}
    assert edit_headers == {"Idempotency-Key", "If-Match"}


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL")
@pytest.mark.asyncio
async def test_postgres_create_run_is_atomic_idempotent_audited_and_tenant_scoped() -> None:
    organization_id, foreign_organization_id = uuid4(), uuid4()
    manager_user, employee_user, foreign_user = uuid4(), uuid4(), uuid4()
    manager_member, employee_member, foreign_member = uuid4(), uuid4(), uuid4()
    foreign_run = uuid4()
    foreign_proposal, foreign_proposal_version = uuid4(), uuid4()
    slug = f"task7-api-{organization_id.hex}"
    password = "Task7Integration123!"
    manager_email = f"manager-{manager_user.hex}@example.test"
    employee_email = f"employee-{employee_user.hex}@example.test"
    settings = Settings(
        environment="test",
        ai_provider="mock",
        local_auth_organization_slug=slug,
        session_cookie_name=f"task7_session_{organization_id.hex}",
    )
    engine = create_database_engine(settings)
    try:
        encoded = PasswordHash.recommended().hash(password)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO organizations (id, slug, name) VALUES "
                    "(:org, :slug, 'Task 7 Tenant'), "
                    "(:foreign_org, :foreign_slug, 'Foreign Tenant')"
                ),
                {
                    "org": organization_id,
                    "slug": slug,
                    "foreign_org": foreign_organization_id,
                    "foreign_slug": f"foreign-task7-{foreign_organization_id.hex}",
                },
            )
            for user_id, email in (
                (manager_user, manager_email),
                (employee_user, employee_email),
                (foreign_user, f"foreign-{foreign_user.hex}@example.test"),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO users "
                        "(id, email_normalized, email_display, display_name, password_hash) "
                        "VALUES (:id, :email, :email, 'Actor', :hash)"
                    ),
                    {"id": user_id, "email": email, "hash": encoded},
                )
            for member_id, user_id, role, org_id in (
                (manager_member, manager_user, "MANAGER", organization_id),
                (employee_member, employee_user, "EMPLOYEE", organization_id),
                (foreign_member, foreign_user, "MANAGER", foreign_organization_id),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO memberships (id, organization_id, user_id, role) "
                        "VALUES (:id, :org, :user, :role)"
                    ),
                    {"id": member_id, "org": org_id, "user": user_id, "role": role},
                )
            await connection.execute(
                text(
                    "INSERT INTO workflow_runs "
                    "(id, organization_id, project_id, requested_by_membership_id, status, "
                    "workflow_name, workflow_version, verifier_version, input_goal_text) "
                    "VALUES (:id, :org, NULL, :member, 'QUEUED', "
                    "'project_planning', '1.0.0', '1.0.0', 'Foreign plan')"
                ),
                {"id": foreign_run, "org": foreign_organization_id, "member": foreign_member},
            )
            await connection.execute(
                text(
                    "INSERT INTO proposals "
                    "(id, organization_id, workflow_run_id, status, current_version_number) "
                    "VALUES (:id, :org, :run, 'DRAFT', 1)"
                ),
                {"id": foreign_proposal, "org": foreign_organization_id, "run": foreign_run},
            )
            await connection.execute(
                text(
                    "INSERT INTO proposal_versions "
                    "(id, organization_id, proposal_id, version_number, "
                    "created_by_membership_id, content, assumptions) "
                    "VALUES (:id, :org, :proposal, 1, :member, "
                    "CAST(:content AS jsonb), CAST(:assumptions AS jsonb))"
                ),
                {
                    "id": foreign_proposal_version,
                    "org": foreign_organization_id,
                    "proposal": foreign_proposal,
                    "member": foreign_member,
                    "content": "{}",
                    "assumptions": "[]",
                },
            )

        app = create_app(settings)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={"email": manager_email, "password": password},
            )
            assert login.status_code == 200
            body = {"message": "Plan a customer conference", "locale": "en"}
            created = await client.post(
                "/api/v1/ai/planning-runs",
                json=body,
                headers={"Idempotency-Key": "task7-create-run-key"},
            )
            replay = await client.post(
                "/api/v1/ai/planning-runs",
                json=body,
                headers={"Idempotency-Key": "task7-create-run-key"},
            )
            run_id = UUID(created.json()["run_id"])
            transaction_factory = PostgreSQLPlanningRunTransactionFactory(
                create_session_factory(engine)
            )
            job_service = JobService(
                transaction_factory=transaction_factory,
                handlers=build_planning_job_handlers(
                    settings,
                    transaction_factory,
                    CurrentActorResolver(
                        CurrentActorService(
                            SqlAlchemyAuthTransactionFactory(create_session_factory(engine))
                        )
                    ),
                ),
                organization_scopes={organization_id},
            )
            assert await job_service.run_once("task7-test-worker", organization_id) is True
            processed = await client.get(f"/api/v1/workflow-runs/{run_id}")
            proposal_snapshot = processed.json()["current_proposal"]
            original_version = await client.get(
                f"/api/v1/proposals/{proposal_snapshot['proposal_id']}/versions/1"
            )
            edited = await client.patch(
                f"/api/v1/proposals/{proposal_snapshot['proposal_id']}",
                json={"content": proposal_snapshot["content"]},
                headers={
                    "Idempotency-Key": "task7-proposal-edit-key",
                    "If-Match": '"1"',
                },
            )
            assert await job_service.run_once("task7-test-worker", organization_id) is True
            revalidated = await client.get(f"/api/v1/workflow-runs/{run_id}")
            short_created = await client.post(
                "/api/v1/ai/planning-runs",
                json={"message": "Plan conference", "locale": "en"},
                headers={"Idempotency-Key": "task7-short-request-key"},
            )
            short_run = UUID(short_created.json()["run_id"])
            assert await job_service.run_once("task7-test-worker", organization_id) is True
            short_result = await client.get(f"/api/v1/workflow-runs/{short_run}")
            foreign = await client.get(f"/api/v1/workflow-runs/{foreign_run}")
            foreign_proposal_read = await client.get(
                f"/api/v1/proposals/{foreign_proposal}/versions/1"
            )
            await client.post("/api/v1/auth/logout")
            await client.post(
                "/api/v1/auth/login",
                json={"email": employee_email, "password": password},
            )
            employee = await client.post(
                "/api/v1/ai/planning-runs",
                json=body,
                headers={"Idempotency-Key": "task7-employee-key"},
            )

        assert created.status_code == 202
        assert replay.status_code == 202
        assert replay.json() == created.json()
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert processed.status_code == 200
        assert processed.json()["status"] == "WAITING_FOR_DECISION"
        assert processed.json()["current_proposal"] is not None
        assert processed.json()["public_timeline"]
        assert original_version.status_code == 200
        assert original_version.json()["version"] == 1
        assert original_version.json()["current_version"] == 1
        assert edited.status_code == 202
        assert edited.headers["etag"] == '"2"'
        assert revalidated.json()["current_proposal"]["version"] == 2
        assert revalidated.json()["current_proposal"]["status"] == "READY_FOR_DECISION"
        assert short_created.status_code == 202
        assert short_result.json()["status"] == "WAITING_FOR_DECISION"
        assert short_result.json()["current_proposal"] is not None
        assert foreign.status_code == 404
        assert foreign_proposal_read.status_code == 404
        assert employee.status_code == 403
        async with engine.connect() as connection:
            counts = (
                await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM workflow_runs WHERE id = :run), "
                        "(SELECT count(*) FROM workflow_jobs WHERE workflow_run_id = :run), "
                        "(SELECT count(*) FROM audit_events "
                        " WHERE resource_id = :run AND action = 'planning_run.created'), "
                        "(SELECT count(*) FROM proposals WHERE workflow_run_id = :run)"
                    ),
                    {"run": run_id},
                )
            ).one()
        assert tuple(counts) == (1, 2, 1, 1)
        async with engine.connect() as connection:
            safety_counts = (
                await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM proposal_versions pv "
                        " JOIN proposals p ON p.id = pv.proposal_id "
                        " WHERE p.workflow_run_id = :run), "
                        "(SELECT count(*) FROM approvals a "
                        " JOIN proposals p ON p.id = a.proposal_id "
                        " WHERE p.workflow_run_id = :run AND a.status = 'PENDING'), "
                        "(SELECT count(*) FROM projects WHERE organization_id = :org), "
                        "(SELECT count(*) FROM tasks WHERE organization_id = :org)"
                    ),
                    {"run": run_id, "org": organization_id},
                )
            ).one()
        assert tuple(safety_counts) == (2, 1, 0, 0)
    finally:
        await engine.dispose()
