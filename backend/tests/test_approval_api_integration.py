"""ASGI and PostgreSQL integration coverage for Task 8 approval decisions."""

from __future__ import annotations

import asyncio
import json
import os
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
from app.modules.planning_runs.application.approval_ports import (
    ApprovalDecisionResult,
    CreatedBusinessIds,
)
from app.modules.planning_runs.application.job_service import JobService
from app.modules.planning_runs.domain.models import (
    ApprovalStateConflictError,
    ApprovalStatus,
    IdempotencyKeyReusedError,
    PlanningRunForbiddenError,
    PlanningRunNotFoundError,
    ProposalStaleError,
    ProposalStatus,
    ProposalValidationError,
    ResourceVersionMismatchError,
)


def _actor(role: MembershipRole = MembershipRole.MANAGER) -> AuthenticatedActor:
    return AuthenticatedActor(
        user_id=uuid4(),
        email="actor@example.test",
        display_name="Actor",
        membership_id=uuid4(),
        organization_id=uuid4(),
        organization_name="Tenant",
        role=role,
    )


class StubApprovalService:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.last_decision: dict[str, Any] | None = None
        self.replayed = False

    async def decide(self, **values: Any) -> ApprovalDecisionResult:
        if self.error is not None:
            raise self.error
        self.last_decision = values
        approve = str(values["decision"]) == "APPROVE"
        return ApprovalDecisionResult(
            approval_id=values["approval_id"],
            approval_status=ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED,
            proposal_id=uuid4(),
            proposal_version=values["expected_proposal_version"],
            proposal_status=ProposalStatus.APPROVED if approve else ProposalStatus.REJECTED,
            workflow_run_id=uuid4(),
            finalization_job_id=uuid4(),
            created=(
                CreatedBusinessIds(
                    project_id=uuid4(),
                    goal_id=uuid4(),
                    milestone_ids=(uuid4(),),
                    task_ids=(uuid4(),),
                    dependency_ids=(uuid4(),),
                    acceptance_criterion_ids=(uuid4(),),
                )
                if approve
                else CreatedBusinessIds()
            ),
            replayed=self.replayed,
        )


def _app(current_actor: AuthenticatedActor, service: StubApprovalService) -> FastAPI:
    app = create_app(
        Settings(environment="test"),
        approval_service=service,  # type: ignore[arg-type]
    )
    app.dependency_overrides[get_authenticated_actor] = lambda: current_actor
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [MembershipRole.MANAGER, MembershipRole.ADMIN])
async def test_approve_contract_returns_typed_created_graph(role: MembershipRole) -> None:
    service = StubApprovalService()
    approval_id = uuid4()
    transport = ASGITransport(app=_app(_actor(role), service))

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/v1/approvals/{approval_id}/decision",
            json={"decision": "APPROVE", "reason": "Reviewed"},
            headers={"Idempotency-Key": "approval-api-key", "If-Match": '"4"'},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["approval"]["id"] == str(approval_id)
    assert body["approval"]["status"] == "APPROVED"
    assert body["proposal"]["version"] == 4
    assert body["created"]["project_id"] is not None
    assert len(body["created"]["task_ids"]) == 1
    assert body["workflow_run_id"] is not None
    assert body["finalization_job_id"] is not None
    assert service.last_decision is not None
    assert service.last_decision["expected_proposal_version"] == 4


@pytest.mark.asyncio
async def test_reject_contract_returns_zero_business_ids() -> None:
    service = StubApprovalService()
    transport = ASGITransport(app=_app(_actor(), service))

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/v1/approvals/{uuid4()}/decision",
            json={"decision": "REJECT", "reason": None},
            headers={"Idempotency-Key": "approval-reject-key", "If-Match": '"2"'},
        )

    assert response.status_code == 200
    assert response.json()["created"] == {
        "project_id": None,
        "goal_id": None,
        "milestone_ids": [],
        "task_ids": [],
        "dependency_ids": [],
        "acceptance_criterion_ids": [],
    }


@pytest.mark.asyncio
async def test_decision_requires_if_match_and_idempotency_key() -> None:
    service = StubApprovalService()
    transport = ASGITransport(app=_app(_actor(), service))
    url = f"/api/v1/approvals/{uuid4()}/decision"

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing_version = await client.post(
            url,
            json={"decision": "APPROVE"},
            headers={"Idempotency-Key": "approval-header-key"},
        )
        missing_key = await client.post(
            url,
            json={"decision": "APPROVE"},
            headers={"If-Match": '"1"'},
        )

    assert missing_version.status_code == 428
    assert missing_version.json()["error"]["code"] == "PRECONDITION_REQUIRED"
    assert missing_key.status_code == 422
    assert missing_key.json()["error"]["code"] == "VALIDATION_FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (ResourceVersionMismatchError(5), 412, "RESOURCE_VERSION_MISMATCH"),
        (ApprovalStateConflictError(), 409, "APPROVAL_STATE_CONFLICT"),
        (IdempotencyKeyReusedError(), 409, "IDEMPOTENCY_KEY_REUSED"),
        (ProposalStaleError(), 422, "PROPOSAL_STALE"),
        (ProposalValidationError(), 422, "VALIDATION_FAILED"),
        (PlanningRunForbiddenError(), 403, "FORBIDDEN"),
        (PlanningRunNotFoundError(), 404, "RESOURCE_NOT_FOUND"),
    ],
)
async def test_structured_decision_errors(error: Exception, status_code: int, code: str) -> None:
    service = StubApprovalService()
    service.error = error
    transport = ASGITransport(app=_app(_actor(), service))

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/v1/approvals/{uuid4()}/decision",
            json={"decision": "APPROVE"},
            headers={"Idempotency-Key": "approval-error-key", "If-Match": '"4"'},
        )

    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code


@pytest.mark.asyncio
async def test_unsafe_decision_exception_is_normalized() -> None:
    service = StubApprovalService()
    service.error = RuntimeError("SQL secret prompt provider traceback")
    transport = ASGITransport(app=_app(_actor(), service), raise_app_exceptions=False)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/v1/approvals/{uuid4()}/decision",
            json={"decision": "APPROVE"},
            headers={"Idempotency-Key": "approval-unsafe-key", "If-Match": '"1"'},
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret" not in response.text.casefold()
    assert "provider" not in response.text.casefold()


def test_openapi_exposes_decision_headers_and_typed_response() -> None:
    schema = _app(_actor(), StubApprovalService()).openapi()
    operation = schema["paths"]["/api/v1/approvals/{approval_id}/decision"]["post"]
    headers = {
        parameter["name"] for parameter in operation["parameters"] if parameter["in"] == "header"
    }

    assert headers == {"Idempotency-Key", "If-Match"}
    assert "200" in operation["responses"]


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL")
@pytest.mark.asyncio
async def test_postgres_approval_is_atomic_idempotent_and_reject_has_no_business_rows() -> None:
    organization_id, foreign_organization_id = uuid4(), uuid4()
    manager_user, employee_user, assignee_user, foreign_user = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    manager_member, employee_member, assignee_member, foreign_member = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    approve_run, approve_proposal, approve_version, approve_approval = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    reject_run, reject_proposal, reject_version, reject_approval = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    concurrent_run, concurrent_proposal, concurrent_version, concurrent_approval = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    failure_run, failure_proposal, failure_version, failure_approval = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    stale_run, stale_proposal, stale_version_id, stale_approval = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    source_project_id = uuid4()
    failure_trigger = f"task8_fail_{failure_approval.hex}"
    slug = f"task8-api-{organization_id.hex}"
    password = "Task8Integration123!"
    manager_email = f"manager-{manager_user.hex}@example.test"
    employee_email = f"employee-{employee_user.hex}@example.test"
    settings = Settings(
        environment="test",
        local_auth_organization_slug=slug,
        session_cookie_name=f"task8_session_{organization_id.hex}",
    )
    engine = create_database_engine(settings)
    content = {
        "project": {
            "title": "Customer conference",
            "description": "Approved plan",
            "start_date": "2026-09-01",
            "due_date": "2026-09-30",
        },
        "goal": {
            "title": "Engage customers",
            "description": None,
            "expected_outcomes": ["300 attendees"],
            "target_date": "2026-09-30",
        },
        "milestones": [
            {
                "ref": "m1",
                "title": "Venue ready",
                "description": None,
                "due_date": "2026-09-15",
            }
        ],
        "project_weeks": [
            {
                "ref": "w1",
                "week_number": 1,
                "start_date": "2026-09-01",
                "end_date": "2026-09-15",
                "objective": "Secure the venue",
            }
        ],
        "tasks": [
            {
                "ref": "t1",
                "project_week_ref": "w1",
                "milestone_ref": "m1",
                "title": "Shortlist venues",
                "description": None,
                "due_date": "2026-09-10",
                "assignee_membership_id": None,
                "required_skill_labels": ["procurement"],
                "estimated_effort_hours": 16,
                "acceptance_criteria": ["Three venues compared"],
            },
            {
                "ref": "t2",
                "project_week_ref": "w1",
                "milestone_ref": "m1",
                "title": "Book venue",
                "description": None,
                "due_date": "2026-09-15",
                "assignee_membership_id": None,
                "required_skill_labels": ["negotiation"],
                "estimated_effort_hours": 8,
                "acceptance_criteria": ["Signed booking received"],
            },
        ],
        "dependencies": [{"predecessor_ref": "t1", "successor_ref": "t2"}],
        "assumptions": [],
    }
    try:
        encoded = PasswordHash.recommended().hash(password)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO organizations (id, slug, name) VALUES "
                    "(:org, :slug, 'Task 8 Tenant'), "
                    "(:foreign_org, :foreign_slug, 'Foreign Tenant')"
                ),
                {
                    "org": organization_id,
                    "slug": slug,
                    "foreign_org": foreign_organization_id,
                    "foreign_slug": f"foreign-task8-{foreign_organization_id.hex}",
                },
            )
            for user_id, email in (
                (manager_user, manager_email),
                (employee_user, employee_email),
                (assignee_user, f"assignee-{assignee_user.hex}@example.test"),
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
                (assignee_member, assignee_user, "EMPLOYEE", organization_id),
                (foreign_member, foreign_user, "MANAGER", foreign_organization_id),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO memberships (id, organization_id, user_id, role) "
                        "VALUES (:id, :org, :user, :role)"
                    ),
                    {"id": member_id, "org": org_id, "user": user_id, "role": role},
                )
            for run_id, proposal_id, version_id, approval_id in (
                (approve_run, approve_proposal, approve_version, approve_approval),
                (reject_run, reject_proposal, reject_version, reject_approval),
                (
                    concurrent_run,
                    concurrent_proposal,
                    concurrent_version,
                    concurrent_approval,
                ),
                (failure_run, failure_proposal, failure_version, failure_approval),
                (stale_run, stale_proposal, stale_version_id, stale_approval),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO workflow_runs "
                        "(id, organization_id, project_id, requested_by_membership_id, status, "
                        "workflow_name, workflow_version, verifier_version, "
                        "input_goal_text, version) "
                        "VALUES (:run, :org, NULL, :manager, 'WAITING_FOR_DECISION', "
                        "'project_planning', '1.0.0', '1.0.0', 'Plan conference', 3)"
                    ),
                    {"run": run_id, "org": organization_id, "manager": manager_member},
                )
                await connection.execute(
                    text(
                        "INSERT INTO proposals "
                        "(id, organization_id, workflow_run_id, status, current_version_number, "
                        "version) VALUES (:proposal, :org, :run, 'READY_FOR_DECISION', 4, 7)"
                    ),
                    {"proposal": proposal_id, "org": organization_id, "run": run_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO proposal_versions "
                        "(id, organization_id, proposal_id, version_number, "
                        "created_by_membership_id, content, assumptions, validation_result, "
                        "workflow_version, prompt_version, schema_version, model_reference, "
                        "verifier_version, creator_type) VALUES "
                        "(:version, :org, :proposal, 4, :manager, CAST(:content AS jsonb), '[]', "
                        "CAST(:validation AS jsonb), '1.0.0', '1.0.0', '1.0.0', 'mock', "
                        "'1.0.0', 'HUMAN_MANAGER')"
                    ),
                    {
                        "version": version_id,
                        "org": organization_id,
                        "proposal": proposal_id,
                        "manager": manager_member,
                        "content": json.dumps(content),
                        "validation": json.dumps(
                            {"can_approve": True, "errors": [], "warnings": []}
                        ),
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO approvals "
                        "(id, organization_id, proposal_id, proposal_version_number, status) "
                        "VALUES (:approval, :org, :proposal, 4, 'PENDING')"
                    ),
                    {"approval": approval_id, "org": organization_id, "proposal": proposal_id},
                )
                await connection.execute(
                    text("UPDATE proposals SET approval_id = :approval WHERE id = :proposal"),
                    {"approval": approval_id, "proposal": proposal_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO workflow_checkpoints "
                        "(id, organization_id, workflow_run_id, node, sequence, state) "
                        "VALUES (:id, :org, :run, 'await_manager_decision', 1, '{}')"
                    ),
                    {"id": uuid4(), "org": organization_id, "run": run_id},
                )
            await connection.execute(
                text(
                    "INSERT INTO projects "
                    "(id, organization_id, name, version, created_by_membership_id, "
                    "updated_by_membership_id) VALUES "
                    "(:id, :org, 'Changed source', 2, :manager, :manager)"
                ),
                {
                    "id": source_project_id,
                    "org": organization_id,
                    "manager": manager_member,
                },
            )
            await connection.execute(
                text(
                    "UPDATE proposal_versions SET source_reference_snapshot = "
                    "CAST(:snapshot AS jsonb) WHERE id = :version"
                ),
                {
                    "version": stale_version_id,
                    "snapshot": json.dumps(
                        [
                            {
                                "resource_type": "PROJECT",
                                "resource_id": str(source_project_id),
                                "version": 1,
                            }
                        ]
                    ),
                },
            )
            await connection.execute(
                text(
                    f"CREATE FUNCTION {failure_trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                    f"BEGIN IF NEW.id = '{failure_approval}'::uuid "
                    "AND NEW.status = 'APPROVED' THEN "
                    "RAISE EXCEPTION 'injected task 8 failure'; END IF; RETURN NEW; END $$"
                )
            )
            await connection.execute(
                text(
                    f"CREATE TRIGGER {failure_trigger} BEFORE UPDATE ON approvals "
                    f"FOR EACH ROW EXECUTE FUNCTION {failure_trigger}()"
                )
            )

        app = create_app(settings)
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as client:
            assert (
                await client.post(
                    "/api/v1/auth/login",
                    json={"email": manager_email, "password": password},
                )
            ).status_code == 200
            headers = {"Idempotency-Key": "task8-approve-key", "If-Match": '"4"'}
            stale_version = await client.post(
                f"/api/v1/approvals/{approve_approval}/decision",
                json={"decision": "APPROVE"},
                headers={"Idempotency-Key": "task8-stale-version", "If-Match": '"3"'},
            )
            approved = await client.post(
                f"/api/v1/approvals/{approve_approval}/decision",
                json={"decision": "APPROVE", "reason": "Reviewed"},
                headers=headers,
            )
            replayed = await client.post(
                f"/api/v1/approvals/{approve_approval}/decision",
                json={"decision": "APPROVE", "reason": "Reviewed"},
                headers=headers,
            )
            rejected = await client.post(
                f"/api/v1/approvals/{reject_approval}/decision",
                json={"decision": "REJECT"},
                headers={"Idempotency-Key": "task8-reject-key", "If-Match": '"4"'},
            )
            concurrent_results = await asyncio.gather(
                client.post(
                    f"/api/v1/approvals/{concurrent_approval}/decision",
                    json={"decision": "APPROVE"},
                    headers={"Idempotency-Key": "task8-concurrent-a", "If-Match": '"4"'},
                ),
                client.post(
                    f"/api/v1/approvals/{concurrent_approval}/decision",
                    json={"decision": "APPROVE"},
                    headers={"Idempotency-Key": "task8-concurrent-b", "If-Match": '"4"'},
                ),
            )
            failed = await client.post(
                f"/api/v1/approvals/{failure_approval}/decision",
                json={"decision": "APPROVE"},
                headers={"Idempotency-Key": "task8-failure-key", "If-Match": '"4"'},
            )
            stale_source = await client.post(
                f"/api/v1/approvals/{stale_approval}/decision",
                json={"decision": "APPROVE"},
                headers={"Idempotency-Key": "task8-stale-source", "If-Match": '"4"'},
            )
            await client.post("/api/v1/auth/logout")
            await client.post(
                "/api/v1/auth/login",
                json={"email": employee_email, "password": password},
            )
            forbidden = await client.post(
                f"/api/v1/approvals/{uuid4()}/decision",
                json={"decision": "APPROVE"},
                headers={"Idempotency-Key": "task8-employee-key", "If-Match": '"4"'},
            )

        assert stale_version.status_code == 412
        assert stale_version.json()["error"]["code"] == "RESOURCE_VERSION_MISMATCH"
        assert approved.status_code == 200, approved.text
        assert replayed.status_code == 200
        assert replayed.json() == approved.json()
        assert replayed.headers["Idempotency-Replayed"] == "true"
        assert rejected.status_code == 200, rejected.text
        assert sorted(response.status_code for response in concurrent_results) == [200, 409]
        assert failed.status_code == 500
        assert failed.json()["error"]["code"] == "INTERNAL_ERROR"
        assert stale_source.status_code == 422
        assert stale_source.json()["error"]["code"] == "PROPOSAL_STALE"
        assert forbidden.status_code == 403
        planning_transactions = PostgreSQLPlanningRunTransactionFactory(
            create_session_factory(engine)
        )
        job_service = JobService(
            transaction_factory=planning_transactions,
            handlers=build_planning_job_handlers(
                settings,
                planning_transactions,
                CurrentActorResolver(
                    CurrentActorService(
                        SqlAlchemyAuthTransactionFactory(create_session_factory(engine))
                    )
                ),
            ),
            organization_scopes={organization_id},
        )
        for _ in range(3):
            assert await job_service.run_once("task8-test-worker", organization_id) is True
        assert await job_service.run_once("task8-test-worker", organization_id) is False
        project_id = UUID(approved.json()["created"]["project_id"])
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP TRIGGER {failure_trigger} ON approvals"))
            await connection.execute(text(f"DROP FUNCTION {failure_trigger}()"))
            approved_counts = (
                await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM projects WHERE id = :project), "
                        "(SELECT count(*) FROM goals WHERE project_id = :project), "
                        "(SELECT count(*) FROM milestones WHERE project_id = :project), "
                        "(SELECT count(*) FROM project_weeks WHERE project_id = :project), "
                        "(SELECT count(*) FROM tasks WHERE project_id = :project), "
                        "(SELECT count(*) FROM tasks WHERE project_id = :project "
                        " AND assignee_membership_id IS NULL), "
                        "(SELECT count(*) FROM task_dependencies d JOIN tasks t "
                        " ON t.id = d.predecessor_task_id WHERE t.project_id = :project), "
                        "(SELECT count(*) FROM acceptance_criteria c JOIN tasks t "
                        " ON t.id = c.task_id WHERE t.project_id = :project), "
                        "(SELECT count(*) FROM outbox_events WHERE aggregate_id = :proposal "
                        " AND event_type = 'planning.proposal_approved.v1'), "
                        "(SELECT count(*) FROM workflow_jobs WHERE workflow_run_id = :run "
                        " AND job_type = 'planning.finalize'), "
                        "(SELECT count(*) FROM audit_events WHERE resource_id = :approval "
                        " AND action = 'approval.decided' AND outcome = 'SUCCEEDED'), "
                        "(SELECT status FROM workflow_runs WHERE id = :run), "
                        "(SELECT count(*) FROM workflow_events WHERE workflow_run_id = :run "
                        " AND event_type = 'workflow.completed')"
                    ),
                    {
                        "project": project_id,
                        "proposal": approve_proposal,
                        "run": approve_run,
                        "approval": approve_approval,
                    },
                )
            ).one()
            rejected_counts = (
                await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT status FROM approvals WHERE id = :approval), "
                        "(SELECT status FROM proposals WHERE id = :proposal), "
                        "(SELECT count(*) FROM projects p JOIN workflow_runs w "
                        " ON w.project_id = p.id WHERE w.id = :run), "
                        "(SELECT count(*) FROM outbox_events WHERE aggregate_id = :proposal "
                        " AND event_type = 'planning.proposal_approved.v1')"
                    ),
                    {"approval": reject_approval, "proposal": reject_proposal, "run": reject_run},
                )
            ).one()
            failed_counts = (
                await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT status FROM approvals WHERE id = :approval), "
                        "(SELECT status FROM proposals WHERE id = :proposal), "
                        "(SELECT count(*) FROM projects p JOIN workflow_runs w "
                        " ON w.project_id = p.id WHERE w.id = :run), "
                        "(SELECT count(*) FROM outbox_events WHERE aggregate_id = :proposal), "
                        "(SELECT count(*) FROM workflow_jobs WHERE workflow_run_id = :run "
                        " AND job_type = 'planning.finalize'), "
                        "(SELECT count(*) FROM audit_events WHERE resource_id = :approval "
                        " AND action = 'approval.decided' AND outcome = 'SUCCEEDED'), "
                        "(SELECT count(*) FROM idempotency_records "
                        " WHERE operation = :operation AND idempotency_key = 'task8-failure-key')"
                    ),
                    {
                        "approval": failure_approval,
                        "proposal": failure_proposal,
                        "run": failure_run,
                        "operation": f"approval.decision:{failure_approval}",
                    },
                )
            ).one()
            stale_counts = (
                await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT status FROM approvals WHERE id = :approval), "
                        "(SELECT status FROM proposals WHERE id = :proposal), "
                        "(SELECT count(*) FROM projects p JOIN workflow_runs w "
                        " ON w.project_id = p.id WHERE w.id = :run), "
                        "(SELECT count(*) FROM audit_events WHERE resource_id = :approval "
                        " AND action = 'approval.decided' AND outcome = 'REJECTED')"
                    ),
                    {
                        "approval": stale_approval,
                        "proposal": stale_proposal,
                        "run": stale_run,
                    },
                )
            ).one()
        async with engine.connect() as connection:
            baseline = await connection.scalar(
                text(
                    "SELECT b.payload FROM weekly_plan_baselines b JOIN project_weeks w "
                    "ON w.organization_id=b.organization_id AND w.id=b.project_week_id "
                    "WHERE w.project_id=:project"
                ),
                {"project": project_id},
            )
            assert baseline is not None
            assert len(baseline["task_entries"]) == 2
            assert {entry["effort_hours"] for entry in baseline["task_entries"]} == {"16", "8"}
            assert sum(len(entry["predecessor_ids"]) for entry in baseline["task_entries"]) == 1
            # Exactly two approved graphs (including concurrent approval); rejected,
            # stale and injected-failure decisions add no baseline or outbox event.
            total = await connection.scalar(
                text("SELECT count(*) FROM weekly_plan_baselines WHERE organization_id=:org"),
                {"org": organization_id},
            )
            assert total == 2
            events = await connection.scalar(
                text(
                    "SELECT count(*) FROM outbox_events WHERE organization_id=:org "
                    "AND event_type='weekly_plan.captured'"
                ),
                {"org": organization_id},
            )
            assert events == 2
        assert tuple(approved_counts) == (1, 1, 1, 1, 2, 2, 1, 2, 1, 1, 1, "COMPLETED", 1)
        assert tuple(rejected_counts) == ("REJECTED", "REJECTED", 0, 0)
        assert tuple(failed_counts) == ("PENDING", "READY_FOR_DECISION", 0, 0, 0, 0, 0)
        assert tuple(stale_counts) == ("SUPERSEDED", "STALE", 0, 1)
    finally:
        await engine.dispose()
