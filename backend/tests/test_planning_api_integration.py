"""PostgreSQL-backed manual planning API, authorization, and audit tests."""

from __future__ import annotations

import os
from collections import Counter
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pwdlib import PasswordHash
from sqlalchemy import text

from app.core.config import Settings
from app.core.database import create_database_engine
from app.main import create_app

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


@pytest.mark.asyncio
async def test_manual_planning_crud_security_concurrency_and_audit() -> None:
    organization_id, foreign_organization_id = uuid4(), uuid4()
    manager_user, employee_user, foreign_user = uuid4(), uuid4(), uuid4()
    manager_member, employee_member, foreign_member = uuid4(), uuid4(), uuid4()
    foreign_project, foreign_task, foreign_task_2 = uuid4(), uuid4(), uuid4()
    foreign_goal, foreign_milestone, foreign_dependency, foreign_criterion = (
        uuid4() for _ in range(4)
    )
    slug = f"planning-api-{organization_id.hex}"
    password = "PlanningIntegration123!"
    emails = {
        "manager": f"manager-{manager_user.hex}@example.test",
        "employee": f"employee-{employee_user.hex}@example.test",
        "foreign": f"foreign-{foreign_user.hex}@example.test",
    }
    settings = Settings(
        environment="test",
        local_auth_organization_slug=slug,
        session_cookie_name=f"planning_session_{organization_id.hex}",
    )
    engine = create_database_engine(settings)
    try:
        encoded = PasswordHash.recommended().hash(password)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO organizations (id, slug, name) VALUES "
                    "(:org, :slug, 'Planning Tenant'), "
                    "(:foreign_org, :foreign_slug, 'Foreign Planning Tenant')"
                ),
                {
                    "org": organization_id,
                    "slug": slug,
                    "foreign_org": foreign_organization_id,
                    "foreign_slug": f"foreign-planning-{foreign_organization_id.hex}",
                },
            )
            for user_id, email, display_name in (
                (manager_user, emails["manager"], "Manager"),
                (employee_user, emails["employee"], "Employee"),
                (foreign_user, emails["foreign"], "Foreign"),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO users "
                        "(id, email_normalized, email_display, display_name, password_hash) "
                        "VALUES (:id, :email, :email, :name, :hash)"
                    ),
                    {"id": user_id, "email": email, "name": display_name, "hash": encoded},
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
                    "INSERT INTO projects (id, organization_id, name, "
                    "created_by_membership_id, updated_by_membership_id) VALUES "
                    "(:id, :org, 'Foreign', :member, :member)"
                ),
                {"id": foreign_project, "org": foreign_organization_id, "member": foreign_member},
            )
            await connection.execute(
                text(
                    "INSERT INTO tasks (id, organization_id, project_id, title, "
                    "assignee_membership_id, status, created_by_membership_id, "
                    "updated_by_membership_id) VALUES "
                    "(:id, :org, :project, 'Foreign', :member, 'TO_DO', :member, :member), "
                    "(:id2, :org, :project, 'Foreign 2', :member, 'TO_DO', :member, :member)"
                ),
                {
                    "id": foreign_task,
                    "id2": foreign_task_2,
                    "org": foreign_organization_id,
                    "project": foreign_project,
                    "member": foreign_member,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO goals (id, organization_id, project_id, title, "
                    "expected_outcomes, created_by_membership_id, updated_by_membership_id) "
                    "VALUES (:id, :org, :project, 'Foreign goal', '[]'::jsonb, :member, :member)"
                ),
                {
                    "id": foreign_goal,
                    "org": foreign_organization_id,
                    "project": foreign_project,
                    "member": foreign_member,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO milestones (id, organization_id, project_id, name, position, "
                    "created_by_membership_id, updated_by_membership_id) "
                    "VALUES (:id, :org, :project, 'Foreign milestone', 1, :member, :member)"
                ),
                {
                    "id": foreign_milestone,
                    "org": foreign_organization_id,
                    "project": foreign_project,
                    "member": foreign_member,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO task_dependencies (id, organization_id, predecessor_task_id, "
                    "successor_task_id, created_by_membership_id, updated_by_membership_id) "
                    "VALUES (:id, :org, :task, :task2, :member, :member)"
                ),
                {
                    "id": foreign_dependency,
                    "org": foreign_organization_id,
                    "task": foreign_task,
                    "task2": foreign_task_2,
                    "member": foreign_member,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO acceptance_criteria (id, organization_id, task_id, text, "
                    "position, created_by_membership_id, updated_by_membership_id) "
                    "VALUES (:id, :org, :task, 'Foreign criterion', 1, :member, :member)"
                ),
                {
                    "id": foreign_criterion,
                    "org": foreign_organization_id,
                    "task": foreign_task,
                    "member": foreign_member,
                },
            )

        app = create_app(settings)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            assert (
                await client.post(
                    "/api/v1/auth/login",
                    json={"email": emails["manager"], "password": password},
                )
            ).status_code == 200
            project = await client.post(
                "/api/v1/projects",
                json={"name": "Import readiness"},
                headers={"Idempotency-Key": "planning-project-create"},
            )
            project_id = project.json()["id"]

            goal_body = {
                "project_id": project_id,
                "title": "  Import 100 shipments  ",
                "description": "  Prepare operations  ",
                "expected_outcomes": ["Customs ready"],
                "target_date": "2026-09-30",
            }
            goal = await client.post(
                "/api/v1/goals",
                json=goal_body,
                headers={"Idempotency-Key": "planning-goal-create-01"},
            )
            goal_replay = await client.post(
                "/api/v1/goals",
                json=goal_body,
                headers={"Idempotency-Key": "planning-goal-create-01"},
            )
            assert goal.status_code == 201
            assert goal.json()["title"] == "Import 100 shipments"
            assert goal_replay.json() == goal.json()
            assert goal_replay.headers["Idempotency-Replayed"] == "true"
            goal_key_reused = await client.post(
                "/api/v1/goals",
                json={**goal_body, "title": "Different"},
                headers={"Idempotency-Key": "planning-goal-create-01"},
            )
            assert goal_key_reused.status_code == 409
            second_goal = await client.post(
                "/api/v1/goals",
                json={**goal_body, "title": "Second goal"},
                headers={"Idempotency-Key": "planning-goal-create-02"},
            )
            assert second_goal.status_code == 422
            foreign_goal_reference = await client.post(
                "/api/v1/goals",
                json={**goal_body, "project_id": str(foreign_project)},
                headers={"Idempotency-Key": "planning-goal-foreign-01"},
            )
            assert foreign_goal_reference.status_code == 422
            assert (await client.get(f"/api/v1/goals?project_id={project_id}")).json()["total"] == 1

            milestone = await client.post(
                "/api/v1/milestones",
                json={
                    "project_id": project_id,
                    "name": "Customs setup",
                    "target_date": "2026-08-31",
                    "position": 1,
                },
                headers={"Idempotency-Key": "planning-milestone-create-01"},
            )
            assert milestone.status_code == 201
            milestone_id = milestone.json()["id"]
            milestone_replay = await client.post(
                "/api/v1/milestones",
                json={
                    "project_id": project_id,
                    "name": "Customs setup",
                    "target_date": "2026-08-31",
                    "position": 1,
                },
                headers={"Idempotency-Key": "planning-milestone-create-01"},
            )
            assert milestone_replay.headers["Idempotency-Replayed"] == "true"
            foreign_milestone_reference = await client.post(
                "/api/v1/milestones",
                json={"project_id": str(foreign_project), "name": "Invisible", "position": 1},
                headers={"Idempotency-Key": "planning-milestone-foreign-01"},
            )
            assert foreign_milestone_reference.status_code == 422

            project_week = await client.post(
                f"/api/v1/projects/{project_id}/weeks",
                json={
                    "week_number": 1,
                    "start_date": "2026-08-01",
                    "end_date": "2026-08-31",
                    "objective": "Complete customs setup",
                },
                headers={"Idempotency-Key": "planning-week-create-01"},
            )
            assert project_week.status_code == 201
            project_week_id = project_week.json()["id"]
            project_week_replay = await client.post(
                f"/api/v1/projects/{project_id}/weeks",
                json={
                    "week_number": 1,
                    "start_date": "2026-08-01",
                    "end_date": "2026-08-31",
                    "objective": "Complete customs setup",
                },
                headers={"Idempotency-Key": "planning-week-create-01"},
            )
            assert project_week_replay.headers["Idempotency-Replayed"] == "true"
            overlapping_week = await client.post(
                f"/api/v1/projects/{project_id}/weeks",
                json={
                    "week_number": 2,
                    "start_date": "2026-08-20",
                    "end_date": "2026-09-05",
                    "objective": "Overlapping work",
                },
                headers={"Idempotency-Key": "planning-week-overlap"},
            )
            assert overlapping_week.status_code == 422

            late_task = await client.post(
                "/api/v1/tasks",
                json={
                    "project_id": project_id,
                    "project_week_id": project_week_id,
                    "milestone_id": milestone_id,
                    "title": "Too late",
                    "estimated_effort_hours": 1,
                    "due_date": "2026-09-01",
                },
                headers={"Idempotency-Key": "planning-task-too-late"},
            )
            assert late_task.status_code == 422

            task_ids: list[str] = []
            for index in (1, 2):
                task = await client.post(
                    "/api/v1/tasks",
                    json={
                        "project_id": project_id,
                        "project_week_id": project_week_id,
                        "milestone_id": milestone_id,
                        "title": f"Customs task {index}",
                        "required_skill_labels": ["customs"],
                        "estimated_effort_hours": 8,
                        "due_date": "2026-08-20",
                    },
                    headers={"Idempotency-Key": f"planning-task-create-0{index}"},
                )
                assert task.status_code == 201
                assert task.json()["milestone_id"] == milestone_id
                task_ids.append(task.json()["id"])

            dependency = await client.post(
                "/api/v1/task-dependencies",
                json={"predecessor_task_id": task_ids[0], "successor_task_id": task_ids[1]},
                headers={"Idempotency-Key": "planning-dependency-create-01"},
            )
            assert dependency.status_code == 201
            dependency_id = dependency.json()["id"]
            dependency_replay = await client.post(
                "/api/v1/task-dependencies",
                json={"predecessor_task_id": task_ids[0], "successor_task_id": task_ids[1]},
                headers={"Idempotency-Key": "planning-dependency-create-01"},
            )
            assert dependency_replay.headers["Idempotency-Replayed"] == "true"
            cycle = await client.post(
                "/api/v1/task-dependencies",
                json={"predecessor_task_id": task_ids[1], "successor_task_id": task_ids[0]},
                headers={"Idempotency-Key": "planning-dependency-cycle-01"},
            )
            assert cycle.status_code == 422
            cross_project_edge = await client.post(
                "/api/v1/task-dependencies",
                json={"predecessor_task_id": task_ids[0], "successor_task_id": str(foreign_task)},
                headers={"Idempotency-Key": "planning-dependency-foreign"},
            )
            assert cross_project_edge.status_code == 422
            assert (await client.get(f"/api/v1/task-dependencies?project_id={project_id}")).json()[
                "total"
            ] == 1

            criterion = await client.post(
                "/api/v1/acceptance-criteria",
                json={"task_id": task_ids[0], "text": "  Form approved  ", "position": 1},
                headers={"Idempotency-Key": "planning-criterion-create-01"},
            )
            assert criterion.status_code == 201
            assert criterion.json()["text"] == "Form approved"
            criterion_id = criterion.json()["id"]
            criterion_replay = await client.post(
                "/api/v1/acceptance-criteria",
                json={"task_id": task_ids[0], "text": "  Form approved  ", "position": 1},
                headers={"Idempotency-Key": "planning-criterion-create-01"},
            )
            assert criterion_replay.headers["Idempotency-Replayed"] == "true"
            duplicate_criterion = await client.post(
                "/api/v1/acceptance-criteria",
                json={"task_id": task_ids[0], "text": "Form approved", "position": 2},
                headers={"Idempotency-Key": "planning-criterion-duplicate"},
            )
            assert duplicate_criterion.status_code == 422
            assert (await client.get(f"/api/v1/goals/{goal.json()['id']}")).status_code == 200
            assert (await client.get(f"/api/v1/milestones/{milestone_id}")).status_code == 200
            assert (
                await client.get(f"/api/v1/task-dependencies/{dependency_id}")
            ).status_code == 200
            assert (
                await client.get(f"/api/v1/acceptance-criteria/{criterion_id}")
            ).status_code == 200
            for path in (
                f"/api/v1/goals/{foreign_goal}",
                f"/api/v1/milestones/{foreign_milestone}",
                f"/api/v1/task-dependencies/{foreign_dependency}",
                f"/api/v1/acceptance-criteria/{foreign_criterion}",
            ):
                assert (await client.get(path)).status_code == 404
            missing_if_match = await client.patch(
                f"/api/v1/acceptance-criteria/{criterion_id}",
                json={"text": "No version"},
                headers={"Idempotency-Key": "planning-criterion-no-etag"},
            )
            assert missing_if_match.status_code == 428

            stale = await client.patch(
                f"/api/v1/milestones/{milestone_id}",
                json={"name": "Stale"},
                headers={"Idempotency-Key": "planning-milestone-stale-01", "If-Match": '"2"'},
            )
            assert stale.status_code == 412
            assert stale.json()["error"]["details"] == {"current_version": 1}

            for path, body, key in (
                (
                    f"/api/v1/goals/{goal.json()['id']}",
                    {"title": "Stale"},
                    "planning-goal-stale-01",
                ),
                (
                    f"/api/v1/task-dependencies/{dependency_id}",
                    {"predecessor_task_id": task_ids[0]},
                    "planning-dependency-stale-01",
                ),
                (
                    f"/api/v1/acceptance-criteria/{criterion_id}",
                    {"text": "Stale"},
                    "planning-criterion-stale-01",
                ),
            ):
                stale_resource = await client.patch(
                    path,
                    json=body,
                    headers={"Idempotency-Key": key, "If-Match": '"2"'},
                )
                assert stale_resource.status_code == 412
                assert stale_resource.json()["error"]["details"] == {"current_version": 1}

            updated_goal = await client.patch(
                f"/api/v1/goals/{goal.json()['id']}",
                json={"description": None},
                headers={"Idempotency-Key": "planning-goal-update-01", "If-Match": '"1"'},
            )
            updated_milestone = await client.patch(
                f"/api/v1/milestones/{milestone_id}",
                json={"name": "Customs ready"},
                headers={"Idempotency-Key": "planning-milestone-update-01", "If-Match": '"1"'},
            )
            updated_dependency = await client.patch(
                f"/api/v1/task-dependencies/{dependency_id}",
                json={"predecessor_task_id": task_ids[1], "successor_task_id": task_ids[0]},
                headers={"Idempotency-Key": "planning-dependency-update-01", "If-Match": '"1"'},
            )
            updated_criterion = await client.patch(
                f"/api/v1/acceptance-criteria/{criterion_id}",
                json={"text": "Form signed off"},
                headers={"Idempotency-Key": "planning-criterion-update-01", "If-Match": '"1"'},
            )
            assert [
                response.status_code
                for response in (
                    updated_goal,
                    updated_milestone,
                    updated_dependency,
                    updated_criterion,
                )
            ] == [200, 200, 200, 200]

            foreign_reference = await client.post(
                "/api/v1/acceptance-criteria",
                json={"task_id": str(foreign_task), "text": "Invisible", "position": 1},
                headers={"Idempotency-Key": "planning-foreign-criterion"},
            )
            assert foreign_reference.status_code == 422

            await client.post("/api/v1/auth/logout")
            assert (
                await client.post(
                    "/api/v1/auth/login",
                    json={"email": emails["employee"], "password": password},
                )
            ).status_code == 200
            assert (await client.get(f"/api/v1/goals?project_id={project_id}")).status_code == 200
            employee_mutations = (
                ("/api/v1/goals", goal_body, "planning-employee-goal"),
                (
                    "/api/v1/milestones",
                    {"project_id": project_id, "name": "Forbidden", "position": 2},
                    "planning-employee-milestone",
                ),
                (
                    "/api/v1/task-dependencies",
                    {"predecessor_task_id": task_ids[0], "successor_task_id": task_ids[1]},
                    "planning-employee-dependency",
                ),
                (
                    "/api/v1/acceptance-criteria",
                    {"task_id": task_ids[0], "text": "Forbidden", "position": 2},
                    "planning-employee-criterion",
                ),
            )
            for path, body, key in employee_mutations:
                forbidden = await client.post(path, json=body, headers={"Idempotency-Key": key})
                assert forbidden.status_code == 403

            await client.post("/api/v1/auth/logout")
            await client.post(
                "/api/v1/auth/login",
                json={"email": emails["manager"], "password": password},
            )
            for index, task_id in enumerate(task_ids, start=1):
                unlinked = await client.patch(
                    f"/api/v1/tasks/{task_id}",
                    json={"milestone_id": None},
                    headers={
                        "Idempotency-Key": f"planning-task-unlink-0{index}",
                        "If-Match": '"1"',
                    },
                )
                assert unlinked.status_code == 200, unlinked.text
            completed_week = await client.patch(
                f"/api/v1/projects/{project_id}/weeks/{project_week_id}",
                json={"status": "COMPLETED"},
                headers={"Idempotency-Key": "planning-week-complete", "If-Match": '"1"'},
            )
            assert completed_week.status_code == 200
            immutable_week = await client.patch(
                f"/api/v1/projects/{project_id}/weeks/{project_week_id}",
                json={"objective": "Rewrite history"},
                headers={"Idempotency-Key": "planning-week-rewrite", "If-Match": '"2"'},
            )
            assert immutable_week.status_code == 422
            deletes = (
                (f"/api/v1/acceptance-criteria/{criterion_id}", '"2"', "planning-delete-ac-01"),
                (f"/api/v1/task-dependencies/{dependency_id}", '"2"', "planning-delete-dep-01"),
                (f"/api/v1/goals/{goal.json()['id']}", '"2"', "planning-delete-goal-01"),
                (f"/api/v1/milestones/{milestone_id}", '"2"', "planning-delete-ms-01"),
            )
            for path, version, key in deletes:
                deleted = await client.delete(
                    path, headers={"If-Match": version, "Idempotency-Key": key}
                )
                assert deleted.status_code == 200
                if "acceptance-criteria" in path:
                    replayed_delete = await client.delete(
                        path, headers={"If-Match": version, "Idempotency-Key": key}
                    )
                    assert replayed_delete.status_code == 200
                    assert replayed_delete.headers["Idempotency-Replayed"] == "true"

        await app.state.database_engine.dispose()
        async with engine.connect() as connection:
            audit = await connection.execute(
                text(
                    "SELECT action, outcome::text FROM audit_events "
                    "WHERE organization_id = :org AND (action LIKE 'goal.%' "
                    "OR action LIKE 'milestone.%' OR action LIKE 'task_dependency.%' "
                    "OR action LIKE 'acceptance_criterion.%')"
                ),
                {"org": organization_id},
            )
            outcomes = Counter((row.action, row.outcome) for row in audit)
            for prefix in ("goal", "milestone", "task_dependency", "acceptance_criterion"):
                assert outcomes[(f"{prefix}.created", "SUCCEEDED")] == 1
                assert outcomes[(f"{prefix}.created", "REJECTED")] >= 1
                assert outcomes[(f"{prefix}.updated", "SUCCEEDED")] == 1
                assert outcomes[(f"{prefix}.updated", "REJECTED")] == 1
                assert outcomes[(f"{prefix}.deleted", "SUCCEEDED")] == 1
    finally:
        async with engine.begin() as connection:
            # Privileged fixture cleanup only, scoped to the random test tenants.
            # PostgreSQL restores trigger state if this transaction fails.
            await connection.execute(
                text("ALTER TABLE weekly_actual_snapshots DISABLE TRIGGER daily_fact_immutable")
            )
            await connection.execute(
                text(
                    "DELETE FROM weekly_actual_snapshots "
                    "WHERE organization_id IN (:org,:foreign_org)"
                ),
                {"org": organization_id, "foreign_org": foreign_organization_id},
            )
            await connection.execute(
                text("ALTER TABLE weekly_actual_snapshots ENABLE TRIGGER daily_fact_immutable")
            )
            await connection.execute(
                text("ALTER TABLE weekly_plan_baselines DISABLE TRIGGER weekly_baseline_immutable")
            )
            await connection.execute(
                text(
                    "DELETE FROM weekly_plan_baselines "
                    "WHERE organization_id IN (:org, :foreign_org)"
                ),
                {"org": organization_id, "foreign_org": foreign_organization_id},
            )
            await connection.execute(
                text("ALTER TABLE weekly_plan_baselines ENABLE TRIGGER weekly_baseline_immutable")
            )
            await connection.execute(
                text("DELETE FROM outbox_events WHERE organization_id IN (:org, :foreign_org)"),
                {"org": organization_id, "foreign_org": foreign_organization_id},
            )
            await connection.execute(
                text(
                    "ALTER TABLE automation_task_scope_history "
                    "DISABLE TRIGGER scope_history_immutable"
                )
            )
            await connection.execute(
                text(
                    "DELETE FROM automation_task_scope_history "
                    "WHERE organization_id IN (:org,:foreign_org)"
                ),
                {"org": organization_id, "foreign_org": foreign_organization_id},
            )
            await connection.execute(
                text(
                    "ALTER TABLE automation_task_scope_history "
                    "ENABLE TRIGGER scope_history_immutable"
                )
            )
            for table in (
                "acceptance_criteria",
                "task_dependencies",
                "tasks",
                "project_weeks",
                "goals",
                "milestones",
                "idempotency_records",
                "audit_events",
                "auth_sessions",
                "projects",
            ):
                await connection.execute(
                    text(f"DELETE FROM {table} WHERE organization_id IN (:org, :foreign_org)"),
                    {"org": organization_id, "foreign_org": foreign_organization_id},
                )
            await connection.execute(
                text("DELETE FROM memberships WHERE organization_id IN (:org, :foreign_org)"),
                {"org": organization_id, "foreign_org": foreign_organization_id},
            )
            await connection.execute(
                text("DELETE FROM users WHERE id IN (:manager, :employee, :foreign)"),
                {"manager": manager_user, "employee": employee_user, "foreign": foreign_user},
            )
            await connection.execute(
                text("DELETE FROM organizations WHERE id IN (:org, :foreign_org)"),
                {"org": organization_id, "foreign_org": foreign_organization_id},
            )
        await engine.dispose()
