"""PostgreSQL-backed explicit Task assignment API behavior."""

import os
from dataclasses import replace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.identity.api.dependencies import get_authenticated_actor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.work.planning.assignment.application.assignment_service import (
    ExplicitTaskAssignmentService,
)
from tests.test_team_recommendation_api_integration import prepare
from tests.test_team_requirements_repository_integration import Case, database_case, headers

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


async def approve_current_actor_for_project(case: Case, client: AsyncClient) -> None:
    payload = await prepare(case, client)
    created = await client.post(
        f"/api/v1/projects/{case.project_id}/team-recommendations",
        headers=headers(),
        json=payload,
    )
    assert created.status_code == 201, created.text
    approved = await client.post(
        f"/api/v1/recommendations/{created.json()['recommendation_id']}/approve",
        headers=headers(1),
        json={"action": "approve"},
    )
    assert approved.status_code == 200, approved.text


@pytest.mark.asyncio
async def test_explicit_assignment_replays_and_writes_one_atomic_audit_and_outbox() -> None:
    from app.modules.work.planning.assignment.adapters.repository import (
        SqlAlchemyExplicitAssignmentTransactionFactory,
    )

    async with (
        database_case() as case,
        AsyncClient(transport=ASGITransport(app=case.app), base_url="http://test") as client,
    ):
        await approve_current_actor_for_project(case, client)
        await case.connection.execute(
            text(
                "UPDATE capacity_entries SET hours=4 "
                "WHERE organization_id=:org AND membership_id=:member"
            ),
            {"org": case.actor.organization_id, "member": case.actor.membership_id},
        )
        factory = async_sessionmaker(
            bind=case.connection,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        case.app.state.explicit_assignment_service = ExplicitTaskAssignmentService(
            SqlAlchemyExplicitAssignmentTransactionFactory(factory)
        )
        url = f"/api/v1/tasks/{case.task_id}/assign"
        request_headers = headers()
        payload = {
            "assignee_membership_id": str(case.actor.membership_id),
            "expected_task_version": 1,
        }

        assigned = await client.post(url, headers=request_headers, json=payload)
        replay = await client.post(url, headers=request_headers, json=payload)
        reused = await client.post(
            url,
            headers=request_headers,
            json={**payload, "assignee_membership_id": str(case.project_id)},
        )

        assert assigned.status_code == 200, assigned.text
        assert assigned.json()["task"]["assignee"]["membership_id"] == str(case.actor.membership_id)
        assert assigned.json()["task"]["version"] == 2
        assert assigned.json()["effective_capacity_hours"] == 4
        assert assigned.json()["workload_before_hours"] == 0
        assert assigned.json()["workload_after_hours"] == 8
        assert assigned.json()["warnings"] == [{"code": "ASSIGNEE_OVER_CAPACITY"}]
        assert replay.json() == assigned.json()
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert reused.status_code == 409
        assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
        assert (
            await case.connection.scalar(
                text("SELECT count(*) FROM audit_events WHERE action='task.assigned.explicit'")
            )
            == 1
        )
        assert (
            await case.connection.scalar(
                text("SELECT count(*) FROM outbox_events WHERE event_type='task.assigned.v1'")
            )
            == 1
        )


@pytest.mark.asyncio
async def test_explicit_assignment_requires_team_membership_version_and_manager_role() -> None:
    from app.modules.work.planning.assignment.adapters.repository import (
        SqlAlchemyExplicitAssignmentTransactionFactory,
    )

    async with (
        database_case() as case,
        AsyncClient(transport=ASGITransport(app=case.app), base_url="http://test") as client,
    ):
        factory = async_sessionmaker(
            bind=case.connection,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        case.app.state.explicit_assignment_service = ExplicitTaskAssignmentService(
            SqlAlchemyExplicitAssignmentTransactionFactory(factory)
        )
        url = f"/api/v1/tasks/{case.task_id}/assign"
        payload = {
            "assignee_membership_id": str(case.actor.membership_id),
            "expected_task_version": 1,
        }
        missing_key = await client.post(url, json=payload)
        invalid_version = await client.post(
            url,
            headers=headers(),
            json={**payload, "expected_task_version": 0},
        )
        assert missing_key.status_code == 422
        assert "error" in missing_key.json()
        assert invalid_version.status_code == 422
        assert "error" in invalid_version.json()
        assert (
            await case.connection.scalar(
                text(
                    "SELECT count(*) FROM audit_events "
                    "WHERE action='task.assignment.transport.rejected'"
                )
            )
            == 2
        )
        not_on_team = await client.post(url, headers=headers(), json=payload)
        assert not_on_team.status_code == 422
        assert not_on_team.json()["error"]["code"] == "VALIDATION_FAILED"

        await approve_current_actor_for_project(case, client)
        stale = await client.post(
            url,
            headers=headers(),
            json={**payload, "expected_task_version": 2},
        )
        assert stale.status_code == 412
        assert stale.json()["error"]["details"] == {"current_version": 1}

        employee = replace(case.actor, role=MembershipRole.EMPLOYEE)
        case.app.dependency_overrides[get_authenticated_actor] = lambda: employee
        denied = await client.post(url, headers=headers(), json=payload)
        assert denied.status_code == 403


@pytest.mark.asyncio
async def test_outbox_failure_rolls_back_task_audit_and_idempotency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    from app.modules.work.planning.assignment.adapters import repository as assignment_repository
    from app.modules.work.planning.assignment.adapters.repository import (
        SqlAlchemyExplicitAssignmentTransactionFactory,
    )

    async with (
        database_case() as case,
        AsyncClient(
            transport=ASGITransport(app=case.app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client,
    ):
        await approve_current_actor_for_project(case, client)
        factory = async_sessionmaker(
            bind=case.connection,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        case.app.state.explicit_assignment_service = ExplicitTaskAssignmentService(
            SqlAlchemyExplicitAssignmentTransactionFactory(factory)
        )
        idempotency_id, audit_id, duplicate_event_id = uuid4(), uuid4(), uuid4()
        await case.connection.execute(
            text(
                "INSERT INTO outbox_events "
                "(id,event_id,organization_id,event_type,aggregate_type,aggregate_id,payload,"
                "status,envelope_version,attempt_count,max_attempts,available_at,occurred_at) "
                "VALUES (:id,:id,:org,'existing.v1','task',:task,'{}'::jsonb,'PENDING','1.0',"
                "0,3,now(),now())"
            ),
            {
                "id": duplicate_event_id,
                "org": case.actor.organization_id,
                "task": case.task_id,
            },
        )
        generated = iter((idempotency_id, audit_id, duplicate_event_id))
        monkeypatch.setattr(assignment_repository, "uuid4", lambda: next(generated))

        failed = await client.post(
            f"/api/v1/tasks/{case.task_id}/assign",
            headers=headers(),
            json={
                "assignee_membership_id": str(case.actor.membership_id),
                "expected_task_version": 1,
            },
        )

        assert failed.status_code == 500
        task_row = (
            await case.connection.execute(
                text("SELECT assignee_membership_id,version FROM tasks WHERE id=:id"),
                {"id": case.task_id},
            )
        ).one()
        assert task_row == (None, 1)
        assert (
            await case.connection.scalar(
                text("SELECT count(*) FROM audit_events WHERE action='task.assigned.explicit'")
            )
            == 0
        )
        assert (
            await case.connection.scalar(
                text("SELECT count(*) FROM idempotency_records WHERE id=:id"),
                {"id": idempotency_id},
            )
            == 0
        )
