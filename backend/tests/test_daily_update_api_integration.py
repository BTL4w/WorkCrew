"""Real RLS and all-or-nothing manual reporting."""

import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.identity.api.dependencies import get_authenticated_actor
from tests.test_evidence_api_integration import Harness, upload
from tests.test_evidence_api_integration import harness as harness
from tests.test_weekly_baseline_integration import weekly_app

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


def daily_app(h: Harness):
    from app.modules.progress.adapters.daily_update_repository import (
        SqlAlchemyDailyUpdateTransactions,
    )
    from app.modules.progress.application.daily_update_service import DailyUpdateService

    app = weekly_app(h)
    sessions = async_sessionmaker(
        bind=h.connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    app.state.daily_update_service = DailyUpdateService(SqlAlchemyDailyUpdateTransactions(sessions))
    app.state.evidence_service = h.service
    app.dependency_overrides[get_authenticated_actor] = lambda: h.actor
    return app


async def seed_task(h: Harness, owner: object = None) -> str:
    project, task = uuid4(), uuid4()
    await h.sql(
        (
            "INSERT INTO projects (id, organization_id, name, "
            "created_by_membership_id, updated_by_membership_id) VALUES "
            "(:id,:org,'Daily',:m,:m)"
        ),
        {"id": project, "org": h.actor.organization_id, "m": h.peer.membership_id},
    )
    await h.sql(
        (
            "INSERT INTO tasks "
            "(id,organization_id,project_id,title,assignee_membership_id,status,"
            "required_skill_labels,created_by_membership_id,updated_by_membership_id) "
            "VALUES (:id,:org,:p,'Prepare','"
        )
        + str(owner or h.actor.membership_id)
        + "','IN_PROGRESS','[]',:m,:m)",
        {"id": task, "org": h.actor.organization_id, "p": project, "m": h.peer.membership_id},
    )
    return str(task)


def body(task: str, **changes: object) -> dict[str, object]:
    return {
        "task_id": task,
        "expected_task_version": 1,
        "expected_progress_version": 0,
        "reported_percent": "99",
        "spent_hours": "3",
        "reporting_date": datetime.now(UTC).date().isoformat(),
        "done_text": "Prepared",
        **changes,
    }


async def draft(client: AsyncClient, items: list[dict[str, object]]):
    response = await client.post(
        "/api/v1/daily-updates/drafts",
        json={"items": items},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def confirm(client: AsyncClient, d: dict[str, object], key: str | None = None):
    return await client.post(
        f"/api/v1/daily-updates/{d['id']}/confirm",
        json={"draft_id": d["id"], "expected_draft_version": d["version"]},
        headers={"Idempotency-Key": key or str(uuid4())},
    )


@pytest.mark.asyncio
async def test_atomic_confirmation_replay_correction_and_unchanged_task(harness: Harness):
    task = await seed_task(harness)
    before = (
        await harness.sql("SELECT row_to_json(tasks) FROM tasks WHERE id=:id", {"id": task})
    ).scalar_one()
    async with AsyncClient(
        transport=ASGITransport(app=daily_app(harness)), base_url="http://test"
    ) as c:
        d = await draft(c, [body(task)])
        assert d["assessment_state"] == "UNAVAILABLE"
        key = str(uuid4())
        first = await confirm(c, d, key)
        assert first.status_code == 201, first.text
        assert (await confirm(c, d, key)).json() == first.json()
        observation = first.json()["observations"][0]
        assert observation["project_week_state"] == "NO_PROJECT_WEEK"
        revised = await draft(
            c,
            [
                body(
                    task,
                    expected_progress_version=1,
                    spent_hours="2",
                    reported_percent="80",
                    corrects_observation_id=observation["id"],
                    correction_reason="Adjusted hours",
                )
            ],
        )
        assert (await confirm(c, revised)).status_code == 201
        history = await c.get("/api/v1/daily-updates", params={"task_id": task})
        assert len(history.json()) == 2
        context = await c.get(f"/api/v1/tasks/{task}/reporting-context")
        assert context.json()["progress_version"] == 2
        assert Decimal(context.json()["reported_percent"]) == 80
    after = (
        await harness.sql("SELECT row_to_json(tasks) FROM tasks WHERE id=:id", {"id": task})
    ).scalar_one()
    assert after == before
    total = (
        await harness.sql(
            (
                "SELECT sum(w.spent_hours) FROM work_logs w WHERE "
                "w.organization_id=:org AND NOT EXISTS (SELECT 1 FROM "
                "task_progress_observations o WHERE "
                "o.organization_id=w.organization_id AND "
                "o.corrects_observation_id=w.observation_id)"
            ),
            {"org": harness.actor.organization_id},
        )
    ).scalar_one()
    assert total == 2
    assert (
        await harness.sql(
            (
                "SELECT count(*) FROM outbox_events WHERE "
                "event_type='daily_update.confirmed' AND organization_id=:org"
            ),
            {"org": harness.actor.organization_id},
        )
    ).scalar_one() == 2


@pytest.mark.asyncio
async def test_evidence_presence_atomic_ownership_and_reassignment(harness: Harness):
    owned = await seed_task(harness)
    other = await seed_task(harness, harness.peer.membership_id)
    app = daily_app(harness)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        invalid = await c.post(
            "/api/v1/daily-updates/drafts",
            json={"items": [body(owned, reported_percent="100")]},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert invalid.status_code == 422
        proof = (await upload(c)).json()
        refs = [{"evidence_id": proof["evidence_id"], "version": 1}]
        d = await draft(c, [body(owned, reported_percent="100", evidence_refs=refs)])
        success = await confirm(c, d)
        assert success.status_code == 201, success.text
        related = (await c.get(f"/api/v1/tasks/{owned}/reporting-context")).json()["evidence_refs"]
        assert related == refs
        d2 = await draft(
            c,
            [
                body(
                    owned,
                    expected_progress_version=1,
                    reported_percent="100",
                    evidence_refs=related,
                    spent_hours="0",
                )
            ],
        )
        assert (await confirm(c, d2)).status_code == 201
        mixed = await c.post(
            "/api/v1/daily-updates/drafts",
            json={"items": [body(owned, expected_progress_version=2), body(other)]},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert mixed.status_code == 404
        stale = await draft(
            c,
            [body(owned, expected_progress_version=2, reported_percent="100", evidence_refs=refs)],
        )
        await harness.sql(
            "UPDATE tasks SET assignee_membership_id=:m,version=version+1 WHERE id=:id",
            {"id": owned, "m": harness.peer.membership_id},
        )
        assert (await confirm(c, stale)).status_code == 404
        app.dependency_overrides[get_authenticated_actor] = lambda: harness.foreign
        assert (await c.get(f"/api/v1/tasks/{owned}/reporting-context")).status_code == 404
    assert (
        await harness.sql(
            "SELECT confirmed_at IS NOT NULL FROM evidence_originals WHERE id=:id",
            {"id": proof["evidence_id"]},
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_mixed_batch_rolls_back_after_reassignment_and_draft_revision(harness: Harness):
    first = await seed_task(harness)
    second = await seed_task(harness)
    app = daily_app(harness)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        d = await draft(c, [body(first), body(second)])
        revised = await c.patch(
            f"/api/v1/daily-updates/{d['id']}/draft",
            json={"expected_version": 1, "items": [body(first, done_text="Revised"), body(second)]},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert revised.status_code == 200, revised.text
        assert revised.json()["version"] == 2
        assert revised.json()["content_hash"] != d["content_hash"]
        assert (await confirm(c, d)).status_code == 409
        await harness.sql(
            "UPDATE tasks SET assignee_membership_id=:m,version=version+1 WHERE id=:id",
            {"m": harness.peer.membership_id, "id": second},
        )
        assert (await confirm(c, revised.json())).status_code == 404
        assert (await c.get(f"/api/v1/tasks/{first}/reporting-context")).json()[
            "progress_version"
        ] == 0
    for table in (
        "daily_updates",
        "task_progress_observations",
        "task_actual_projections",
        "work_logs",
        "daily_update_evidence_links",
        "weekly_actual_snapshots",
    ):
        assert (
            await harness.sql(
                f"SELECT count(*) FROM {table} WHERE organization_id=:org",
                {"org": harness.actor.organization_id},
            )
        ).scalar_one() == 0
    assert (
        await harness.sql(
            (
                "SELECT count(*) FROM audit_events WHERE organization_id=:org AND "
                "action='daily_update.rejected'"
            ),
            {"org": harness.actor.organization_id},
        )
    ).scalar_one() == 2


@pytest.mark.asyncio
async def test_weekly_actuals_freeze_and_late_correction_preserves_final(harness: Harness):
    task = await seed_task(harness)
    project = (
        await harness.sql("SELECT project_id FROM tasks WHERE id=:id", {"id": task})
    ).scalar_one()
    week = uuid4()
    await harness.sql(
        (
            "INSERT INTO "
            "project_weeks(id,organization_id,project_id,week_number,start_date,end_date,"
            "objective,status,version,created_by_membership_id,updated_by_membership_id) "
            "VALUES(:id,:org,:project,1,'2026-09-28','2026-10-02','Report','ACTIVE',1,:m,:m)"
        ),
        {
            "id": week,
            "org": harness.actor.organization_id,
            "project": project,
            "m": harness.peer.membership_id,
        },
    )
    await harness.sql(
        "UPDATE tasks SET project_week_id=:week,estimated_effort_hours=2 WHERE id=:id",
        {"week": week, "id": task},
    )
    app = daily_app(harness)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # Capture the authorized manual plan before observations exist.
        from app.modules.progress.adapters.progress_repository import capture_project_baselines

        sessions = async_sessionmaker(
            bind=harness.connection,
            class_=AsyncSession,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        async with sessions.begin() as session:
            await capture_project_baselines(session, harness.peer, project)
        before = (
            await harness.sql(
                "SELECT payload FROM weekly_plan_baselines WHERE project_week_id=:week",
                {"week": week},
            )
        ).scalar_one()
        d = await draft(c, [body(task, reported_percent="50", remaining_hours="1")])
        report = await confirm(c, d)
        assert report.status_code == 201, report.text
        assert (
            await harness.sql(
                "SELECT payload FROM weekly_plan_baselines WHERE project_week_id=:week",
                {"week": week},
            )
        ).scalar_one() == before
        app.dependency_overrides[get_authenticated_actor] = lambda: harness.peer
        url = f"/api/v1/projects/{project}/weeks/{week}/progress"
        actuals = (await c.get(url)).json()
        assert Decimal(actuals["current_plan"]["reported_percent"]) == 50
        assert Decimal(actuals["current_plan"]["remaining_hours"]) == 1
        completed = await c.patch(
            f"/api/v1/projects/{project}/weeks/{week}",
            json={"status": "COMPLETED"},
            headers={"Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
        )
        assert completed.status_code == 200, completed.text
        final = (await c.get(url)).json()
        assert final["sealed"] is True
        app.dependency_overrides[get_authenticated_actor] = lambda: harness.actor
        d2 = await draft(
            c,
            [
                body(
                    task,
                    expected_progress_version=1,
                    reported_percent="60",
                    spent_hours="2",
                    corrects_observation_id=report.json()["observations"][0]["id"],
                    correction_reason="Late correction",
                )
            ],
        )
        late = await confirm(c, d2)
        assert late.status_code == 201, late.text
        assert late.json()["observations"][0]["late"] is True
        app.dependency_overrides[get_authenticated_actor] = lambda: harness.peer
        assert (await c.get(url)).json() == final
    kinds = (
        (
            await harness.sql(
                "SELECT kind FROM weekly_actual_snapshots WHERE project_week_id=:week "
                "ORDER BY captured_at",
                {"week": week},
            )
        )
        .scalars()
        .all()
    )
    assert kinds == ["CURRENT", "FINAL", "LATE"]


@pytest.mark.asyncio
async def test_manual_submit_cannot_bypass_assessment_or_evidence_checks(harness: Harness):
    task = await seed_task(harness)
    async with AsyncClient(
        transport=ASGITransport(app=daily_app(harness)), base_url="http://test"
    ) as c:
        d = await draft(c, [body(task)])
        command = {"draft_id": d["id"], "expected_draft_version": 1, "assessment_id": str(uuid4())}
        for url in ("/api/v1/daily-updates", f"/api/v1/daily-updates/{d['id']}/confirm"):
            response = await c.post(url, json=command, headers={"Idempotency-Key": str(uuid4())})
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "ASSESSMENT_UNAVAILABLE"
        command.pop("assessment_id")
        no_key = await c.post("/api/v1/daily-updates", json=command)
        assert no_key.status_code == 400
        fake = await c.post(
            "/api/v1/daily-updates",
            json={**command, "assessment_state": "PASSED"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert fake.status_code == 422
        response = await c.post(
            "/api/v1/daily-updates", json=command, headers={"Idempotency-Key": str(uuid4())}
        )
        assert response.status_code == 201, response.text
        assert response.json()["assessment_state"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_invalid_transport_is_audited_and_shared_new_evidence_links_atomically(
    harness: Harness,
):
    first = await seed_task(harness)
    second = await seed_task(harness)
    async with AsyncClient(
        transport=ASGITransport(app=daily_app(harness)), base_url="http://test"
    ) as c:
        response = await c.post(
            "/api/v1/daily-updates/drafts",
            json={"items": []},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 422
        audits = (
            await harness.sql(
                "SELECT count(*) FROM audit_events WHERE organization_id=:org "
                "AND action='daily_update.rejected'",
                {"org": harness.actor.organization_id},
            )
        ).scalar_one()
        assert audits == 1
        response = await c.post(
            "/api/v1/daily-updates/drafts",
            json={"items": [body(first)]},
            headers={"Idempotency-Key": "x" * 129},
        )
        assert response.status_code == 400
        proof = (await upload(c)).json()
        refs = [{"evidence_id": proof["evidence_id"], "version": 1}]
        d = await draft(
            c,
            [
                body(first, reported_percent="100", evidence_refs=refs),
                body(second, reported_percent="100", evidence_refs=refs),
            ],
        )
        response = await confirm(c, d)
        assert response.status_code == 201, response.text
        assert len(response.json()["observations"]) == 2
        assert (
            await harness.sql(
                "SELECT count(*) FROM daily_update_evidence_links WHERE organization_id=:org",
                {"org": harness.actor.organization_id},
            )
        ).scalar_one() == 2


@pytest.mark.asyncio
async def test_reassignment_backdated_report_preserves_newer_actual_and_private_history(
    harness: Harness,
):
    from datetime import timedelta

    from app.modules.identity.domain.auth import AuthenticatedActor
    from app.modules.organization.domain.roles import MembershipRole

    task = await seed_task(harness)
    user, member = uuid4(), uuid4()
    await harness.sql(
        "INSERT INTO users(id,email_normalized,email_display,display_name,password_hash) "
        "VALUES (:id,:email,:email,'New owner','unused')",
        {"id": user, "email": f"{user}@example.test"},
    )
    await harness.sql(
        "INSERT INTO memberships(id,organization_id,user_id,role) "
        "VALUES (:id,:org,:user,'EMPLOYEE')",
        {"id": member, "org": harness.actor.organization_id, "user": user},
    )
    new_owner = AuthenticatedActor(
        user,
        f"{user}@example.test",
        "New owner",
        member,
        harness.actor.organization_id,
        "Tenant",
        MembershipRole.EMPLOYEE,
    )
    app = daily_app(harness)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        d = await draft(c, [body(task, reported_percent="80")])
        current = await confirm(c, d)
        assert current.status_code == 201, current.text
        original_id = current.json()["observations"][0]["id"]
        await harness.sql(
            "UPDATE tasks SET assignee_membership_id=:m,version=2 WHERE id=:id",
            {"m": member, "id": task},
        )
        app.dependency_overrides[get_authenticated_actor] = lambda: new_owner
        context = (await c.get(f"/api/v1/tasks/{task}/reporting-context")).json()
        assert context["progress_version"] == 1
        assert (await c.get("/api/v1/daily-updates", params={"task_id": task})).json() == []
        d2 = await draft(
            c,
            [
                body(
                    task,
                    expected_task_version=2,
                    expected_progress_version=1,
                    reported_percent="20",
                    spent_hours="0",
                    reporting_date=(datetime.now(UTC).date() - timedelta(days=1)).isoformat(),
                    correction_reason="Historical report before reassignment",
                )
            ],
        )
        backdated = await confirm(c, d2)
        assert backdated.status_code == 201, backdated.text
        pointer = (
            await harness.sql(
                "SELECT observation_id FROM task_actual_projections WHERE task_id=:id", {"id": task}
            )
        ).scalar_one()
        assert str(pointer) == original_id
        # Only current actuals are exposed to the assignee/Manager; history stays private.
        from sqlalchemy import text

        for actor, permitted in (
            (new_owner, True),
            (harness.actor, False),
            (harness.foreign, False),
        ):
            await harness.sql("SET LOCAL ROLE app_runtime")
            await harness.connection.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true), "
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            result = (
                await harness.connection.execute(
                    text(
                        "SELECT observation_id,reported_percent "
                        "FROM public.read_task_reported_actual(:task)"
                    ),
                    {"task": task},
                )
            ).one_or_none()
            if permitted:
                assert result is not None and str(result[0]) == original_id and result[1] == 80
            else:
                assert result is None
