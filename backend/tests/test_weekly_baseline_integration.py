"""Atomic manual baseline capture and permission-safe weekly API."""

import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.config import Settings
from app.main import create_app
from app.modules.identity.api.dependencies import get_authenticated_actor
from tests.test_evidence_api_integration import Harness
from tests.test_evidence_api_integration import harness as harness

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


def weekly_app(harness: Harness):
    manager = harness.peer
    app = create_app(Settings(environment="test"))
    app.dependency_overrides[get_authenticated_actor] = lambda: manager
    # Application transactions share the rollback-isolated connection.
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.modules.work.adapters.project_repository import SqlAlchemyProjectTransactionFactory
    from app.modules.work.adapters.task_repository import SqlAlchemyTaskTransactionFactory
    from app.modules.work.application.project_service import ProjectService
    from app.modules.work.application.task_service import TaskService
    from app.modules.work.planning.adapters.manual_repository import (
        SqlAlchemyManualPlanningTransactionFactory,
    )
    from app.modules.work.planning.application.manual_service import ManualPlanningService

    sessions = async_sessionmaker(
        bind=harness.connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    from app.modules.progress.adapters.progress_repository import (
        SqlAlchemyProgressTransactionFactory,
    )
    from app.modules.progress.application.weekly_progress_service import WeeklyProgressService

    app.state.weekly_progress_service = WeeklyProgressService(
        SqlAlchemyProgressTransactionFactory(sessions)
    )
    app.state.project_service = ProjectService(SqlAlchemyProjectTransactionFactory(sessions))
    app.state.task_service = TaskService(SqlAlchemyTaskTransactionFactory(sessions))
    app.state.manual_planning_service = ManualPlanningService(
        SqlAlchemyManualPlanningTransactionFactory(sessions)
    )
    return app


@pytest.mark.asyncio
async def test_manual_plan_edit_preserves_immutable_before_state(harness: Harness):
    manager = harness.peer
    app = weekly_app(harness)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        project = (
            await client.post(
                "/api/v1/projects",
                json={"name": "Weekly baseline"},
                headers={"Idempotency-Key": str(uuid4())},
            )
        ).json()
        week_response = await client.post(
            f"/api/v1/projects/{project['id']}/weeks",
            json={
                "week_number": 1,
                "start_date": "2026-09-28",
                "end_date": "2026-10-02",
                "objective": "Deliver",
            },
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert week_response.status_code == 201, week_response.text
        week = week_response.json()
        body: dict[str, object] = {
            "project_id": project["id"],
            "project_week_id": week["id"],
            "title": "First",
            "estimated_effort_hours": 2,
            "required_skill_labels": [],
        }
        task_response = await client.post(
            "/api/v1/tasks", json=body, headers={"Idempotency-Key": str(uuid4())}
        )
        assert task_response.status_code == 201, task_response.text
        task = task_response.json()
        rows = (
            (
                await harness.sql(
                    "SELECT payload FROM weekly_plan_baselines WHERE organization_id=:org AND "
                    "project_week_id=:week ORDER BY sequence",
                    {"org": manager.organization_id, "week": week["id"]},
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert rows[-1]["task_entries"][0]["effort_hours"] == "2"
        key = str(uuid4())
        headers = {"Idempotency-Key": key, "If-Match": f'"{task["version"]}"'}
        changed = await client.patch(
            f"/api/v1/tasks/{task['id']}", json={"estimated_effort_hours": 6}, headers=headers
        )
        assert changed.status_code == 200, changed.text
        replay = await client.patch(
            f"/api/v1/tasks/{task['id']}", json={"estimated_effort_hours": 6}, headers=headers
        )
        assert replay.status_code == 200
        stale = await client.patch(
            f"/api/v1/tasks/{task['id']}",
            json={"estimated_effort_hours": 8},
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        assert stale.status_code == 412
        rows = (
            (
                await harness.sql(
                    "SELECT payload FROM weekly_plan_baselines WHERE organization_id=:org AND "
                    "project_week_id=:week ORDER BY sequence",
                    {"org": manager.organization_id, "week": week["id"]},
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 3
        assert rows[-2]["task_entries"][0]["effort_hours"] == "2"
        assert rows[-1]["task_entries"][0]["effort_hours"] == "6"

        url = f"/api/v1/projects/{project['id']}/weeks/{week['id']}/progress"
        response = await client.get(url)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["current_plan"]["reported_percent"] is None
        assert data["current_plan"]["known_effort_fraction"] == "0"
        assert data["current_plan"]["total_effort_hours"] == "6"
        assert data["original"]["baseline"]["task_entries"] == []
        assert data["added_task_ids"] == [task["id"]]
        assert response.headers["cache-control"] == "private, no-store"
        app.dependency_overrides[get_authenticated_actor] = lambda: harness.actor
        assert (await client.get(url)).status_code == 403
        app.dependency_overrides[get_authenticated_actor] = lambda: harness.foreign
        assert (await client.get(url)).status_code == 404
        app.dependency_overrides[get_authenticated_actor] = lambda: manager
        assert (
            await client.get(f"/api/v1/projects/{uuid4()}/weeks/{week['id']}/progress")
        ).status_code == 404
        # Seal once; historical evaluation time cannot drift as the calendar advances.
        closed = await client.patch(
            f"/api/v1/projects/{project['id']}/weeks/{week['id']}",
            json={"status": "COMPLETED"},
            headers={"Idempotency-Key": str(uuid4()), "If-Match": f'"{week["version"]}"'},
        )
        assert closed.status_code == 200, closed.text
        sealed = (await client.get(url)).json()
        assert sealed["sealed"] is True
        assert (
            sealed["current_plan"]["evaluated_at"]
            == sealed["current_plan"]["baseline"]["sealed_at"]
        )
        assert (await client.get(url)).json() == sealed
        # Real RLS: employee and foreign manager cannot select this tenant's baseline.
        await harness.sql("SET LOCAL ROLE app_runtime")
        await harness.connection.execute(
            text(
                "SELECT set_config('app.organization_id', :org, true), "
                "set_config('app.membership_id', :member, true)"
            ),
            {"org": str(manager.organization_id), "member": str(harness.actor.membership_id)},
        )
        assert (
            await harness.connection.execute(text("SELECT id FROM weekly_plan_baselines"))
        ).all() == []
        await harness.sql("SET LOCAL ROLE app_runtime")
        await harness.connection.execute(
            text(
                "SELECT set_config('app.organization_id', :org, true), "
                "set_config('app.membership_id', :member, true)"
            ),
            {
                "org": str(harness.foreign.organization_id),
                "member": str(harness.foreign.membership_id),
            },
        )
        assert (
            await harness.connection.execute(
                text("SELECT id FROM weekly_plan_baselines WHERE project_week_id=:week"),
                {"week": week["id"]},
            )
        ).all() == []
        # Revocation is checked at read time even if the cached actor says Manager.
        await harness.sql(
            "UPDATE memberships SET is_active=false WHERE id=:id", {"id": manager.membership_id}
        )
        assert (await client.get(url)).status_code == 403


@pytest.mark.asyncio
async def test_activation_backfills_entry_baseline_and_database_guards(harness: Harness) -> None:
    """Run the real migration against a private pre-activation schema."""
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.engine import Connection
    from sqlalchemy.exc import DBAPIError

    schema = f"weekly_backfill_{uuid4().hex}"
    await harness.sql(f"CREATE SCHEMA {schema}")
    await harness.sql(f"SET LOCAL search_path TO {schema}, public")
    for table in ("project_weeks", "tasks", "task_dependencies"):
        await harness.sql(f"CREATE TABLE {schema}.{table} (LIKE public.{table} INCLUDING ALL)")
    week_id, project_id, task_id = uuid4(), uuid4(), uuid4()
    actor = harness.peer
    parameters: dict[str, object] = {
        "week": week_id,
        "project": project_id,
        "task": task_id,
        "org": actor.organization_id,
        "member": actor.membership_id,
    }
    await harness.sql(
        "INSERT INTO project_weeks (id, organization_id, project_id, week_number, "
        "start_date, end_date, objective, status, created_by_membership_id, "
        "updated_by_membership_id) VALUES (:week, :org, :project, 1, '2026-09-28', "
        "'2026-10-02', 'Existing approved plan', 'COMPLETED', :member, :member)",
        parameters,
    )
    await harness.sql(
        "INSERT INTO tasks (id, organization_id, project_id, project_week_id, "
        "title, status, estimated_effort_hours, created_by_membership_id, "
        "updated_by_membership_id) VALUES (:task, :org, :project, :week, 'Existing "
        "task', 'DONE', 6, :member, :member)",
        parameters,
    )
    path = Path(__file__).parents[1] / "alembic/versions/0019_weekly_progress_baselines.py"
    specification = importlib.util.spec_from_file_location("weekly_baseline_migration", path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)

    def upgrade(connection: Connection) -> None:
        module.__dict__["op"] = Operations(MigrationContext.configure(connection))
        module.upgrade()

    await harness.connection.run_sync(upgrade)
    row = (await harness.sql("SELECT kind, sealed_at, payload FROM weekly_plan_baselines")).one()
    assert row.kind == "ENTRY"
    assert row.sealed_at is not None
    assert row.payload["task_entries"][0]["effort_hours"] == "6"
    assert row.payload["task_entries"][0]["captured_status"] == "DONE"
    # Even the migration-owner role cannot rewrite or directly delete snapshots.
    for statement in (
        "UPDATE weekly_plan_baselines SET kind='MANUAL'",
        "DELETE FROM weekly_plan_baselines",
        "DELETE FROM project_weeks",
    ):
        async with harness.connection.begin_nested() as savepoint:
            with pytest.raises(DBAPIError, match="immutable weekly plan baseline"):
                await harness.connection.execute(text(statement))
            await savepoint.rollback()
    async with harness.connection.begin_nested() as savepoint:
        with pytest.raises(DBAPIError, match="invalid weekly plan task reference"):
            await harness.connection.execute(
                text(
                    "INSERT INTO weekly_plan_baselines "
                    "(id, organization_id, project_week_id, sequence, kind, "
                    "captured_at, sealed_at, payload) "
                    "SELECT gen_random_uuid(), organization_id, project_week_id, 2, kind, "
                    "captured_at, "
                    "sealed_at, jsonb_set(payload, '{task_entries,0,task_id}', "
                    "to_jsonb(CAST(:id AS text))) "
                    "FROM weekly_plan_baselines WHERE sequence=1"
                ),
                {"id": str(uuid4())},
            )
        await savepoint.rollback()
    await harness.sql("SET LOCAL search_path TO public")


@pytest.mark.asyncio
@pytest.mark.parametrize("check_deletion", [True, False])
async def test_moved_tasks_keep_history_and_seal_current_status(
    harness: Harness, check_deletion: bool
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.modules.progress.adapters.progress_repository import capture_project_baselines

    actor = harness.peer
    project, week, next_week, task = (uuid4() for _ in range(4))
    parameters: dict[str, object] = {
        "org": actor.organization_id,
        "member": actor.membership_id,
        "project": project,
        "week": week,
        "next": next_week,
        "task": task,
    }
    await harness.sql(
        "INSERT INTO projects (id, organization_id, name, created_by_membership_id, "
        "updated_by_membership_id) VALUES (:project, :org, 'History', :member, "
        ":member)",
        parameters,
    )
    await harness.sql(
        "INSERT INTO project_weeks (id, organization_id, project_id, week_number, "
        "start_date, end_date, objective, status, created_by_membership_id, "
        "updated_by_membership_id) VALUES (:week, :org, :project, 1, '2026-09-28', "
        "'2026-10-02', 'Original', 'ACTIVE', :member, :member), (:next, :org, "
        ":project, 2, '2026-10-05', '2026-10-09', 'Next', 'PLANNED', :member, "
        ":member)",
        parameters,
    )
    await harness.sql(
        "INSERT INTO tasks (id, organization_id, project_id, project_week_id, "
        "title, status, estimated_effort_hours, created_by_membership_id, "
        "updated_by_membership_id) VALUES (:task, :org, :project, :week, 'Moved "
        "work', 'TO_DO', 2, :member, :member)",
        parameters,
    )
    sessions = async_sessionmaker(
        bind=harness.connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    async with sessions.begin() as session:
        await capture_project_baselines(session, actor, project, "ENTRY")
    await harness.sql("UPDATE tasks SET status='DONE' WHERE id=:task", parameters)
    app = weekly_app(harness)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        moved = await client.patch(
            f"/api/v1/tasks/{task}",
            json={"project_week_id": str(next_week)},
            headers={"Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
        )
        assert moved.status_code == 200, moved.text
        url = f"/api/v1/projects/{project}/weeks/{week}/progress"
        before = (await client.get(url)).json()
        assert before["original"]["task_actuals"][0]["status"] == "DONE"
        if check_deletion:
            deleted = await client.delete(
                f"/api/v1/projects/{project}/weeks/{week}",
                headers={"Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
            )
            assert deleted.status_code == 422, (
                "Removing all live tasks must not erase nonempty history"
            )
            assert (await client.get(url)).status_code == 200
        closed = await client.patch(
            f"/api/v1/projects/{project}/weeks/{week}",
            json={"status": "COMPLETED"},
            headers={"Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
        )
        assert closed.status_code == 200, closed.text
        after = (await client.get(url)).json()
        assert after["original"]["task_actuals"][0]["status"] == "DONE"
        assert after["original"]["baseline"] == before["original"]["baseline"]
        await harness.sql("UPDATE tasks SET status='TO_DO' WHERE id=:task", parameters)
        assert (await client.get(url)).json() == after
