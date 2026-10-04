"""Real SQL report capture, replay, authorization and immutable history."""

import asyncio
import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.database import create_database_engine
from app.main import create_app
from app.modules.identity.api.dependencies import get_authenticated_actor
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.reporting.adapters.transaction import ReportTransactions
from app.modules.reporting.application.report_service import ReportService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


@dataclass
class ReportHarness:
    engine: AsyncEngine
    service: ReportService
    actor: AuthenticatedActor
    employee: AuthenticatedActor
    foreign: AuthenticatedActor
    project_id: UUID
    task_id: UUID

    def app(self, actor: AuthenticatedActor | None = None) -> FastAPI:
        app = create_app(
            Settings(environment="test", ai_provider="mock"), reporting_service=self.service
        )
        app.dependency_overrides[get_authenticated_actor] = lambda: actor or self.actor
        return app

    async def sql(self, statement: str, values: dict[str, Any] | None = None) -> Any:
        async with self.engine.begin() as connection:
            return await connection.execute(text(statement), values or {})

    def body(self) -> dict[str, object]:
        return {
            "project_id": str(self.project_id),
            "kind": "DAILY",
            "timezone": "UTC",
            "period_start": datetime.now(UTC).date().isoformat(),
            "locale": "en",
        }


@pytest_asyncio.fixture
async def report_harness() -> AsyncGenerator[ReportHarness]:
    engine = create_database_engine(Settings(environment="test"))
    actors: list[AuthenticatedActor] = []
    first_org = uuid4()
    project, task = uuid4(), uuid4()
    async with engine.begin() as connection:
        for index in range(3):
            org = first_org if index < 2 else uuid4()
            if index != 1:
                await connection.execute(
                    text(
                        "INSERT INTO organizations(id,slug,name) VALUES (:id,:slug,'Report tenant')"
                    ),
                    {"id": org, "slug": f"report-{org}"},
                )
            user, member = uuid4(), uuid4()
            email = f"{user}@example.test"
            await connection.execute(
                text(
                    "INSERT INTO users(id,email_normalized,email_display,"
                    "display_name,password_hash) VALUES (:id,:email,:email,'Reporter','unused')"
                ),
                {"id": user, "email": email},
            )
            role = MembershipRole.EMPLOYEE if index == 1 else MembershipRole.MANAGER
            await connection.execute(
                text(
                    "INSERT INTO memberships(id,organization_id,user_id,role) "
                    "VALUES (:id,:org,:user,:role)"
                ),
                {"id": member, "org": org, "user": user, "role": role},
            )
            actors.append(AuthenticatedActor(user, email, "Reporter", member, org, "Tenant", role))
        await connection.execute(
            text(
                "INSERT INTO projects(id,organization_id,name,"
                "created_by_membership_id,updated_by_membership_id) "
                "VALUES (:id,:org,'Conference',:m,:m)"
            ),
            {"id": project, "org": first_org, "m": actors[0].membership_id},
        )
        await connection.execute(
            text(
                "INSERT INTO tasks(id,organization_id,project_id,title,status,"
                "estimated_effort_hours,required_skill_labels,created_by_membership_id,"
                "updated_by_membership_id) VALUES (:id,:org,:p,'Survey','IN_PROGRESS',4,'[]',:m,:m)"
            ),
            {"id": task, "org": first_org, "p": project, "m": actors[0].membership_id},
        )
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    h = ReportHarness(
        engine,
        ReportService(ReportTransactions(sessions, "UTC")),
        actors[0],
        actors[1],
        actors[2],
        project,
        task,
    )
    try:
        yield h
    finally:
        # Fixtures are committed for separate connections/concurrency; remove only their own orgs.
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL session_replication_role=replica"))
            for table in (
                "reporting_window_reporters",
                "reporting_windows",
                "automation_schedule_recipients",
                "automation_schedule_versions",
                "automation_schedule_drafts",
                "automation_schedules",
                "automation_task_scope_history",
                "capacity_entries",
                "leave_entries",
                "risk_notifications",
                "risk_review_events",
                "risk_refresh_jobs",
                "risk_assessments",
                "blocker_transitions",
                "blocker_evidence_links",
                "blockers",
                "weekly_plan_baselines",
                "project_weeks",
                "work_logs",
                "task_actual_projections",
                "task_progress_observations",
                "daily_updates",
                "daily_update_draft_revisions",
                "daily_update_drafts",
                "report_snapshot_sources",
                "report_snapshot_receipts",
                "report_versions",
                "report_metric_snapshots",
                "reports",
                "outbox_events",
                "audit_events",
                "idempotency_records",
                "tasks",
                "projects",
                "memberships",
            ):
                await connection.execute(
                    text(f"DELETE FROM {table} WHERE organization_id IN (:a,:b)"),
                    {"a": first_org, "b": actors[2].organization_id},
                )
            for actor in actors:
                await connection.execute(
                    text("DELETE FROM users WHERE id=:id"), {"id": actor.user_id}
                )
            await connection.execute(
                text("DELETE FROM organizations WHERE id IN (:a,:b)"),
                {"a": first_org, "b": actors[2].organization_id},
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_replay_conflict_and_rejected_audit(report_harness: ReportHarness) -> None:
    h = report_harness
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        headers = {"Idempotency-Key": str(uuid4())}
        first = await client.post("/api/v1/reports", json=h.body(), headers=headers)
        assert first.status_code == 201, first.text
        report = first.json()
        assert first.headers["ETag"] == '"1"'
        assert report["snapshot"]["metrics"]["tasks.status.total_count"]["value"] == "1"
        assert report["snapshot"]["metrics"]["tasks.status.done_count"]["value"] == "0"
        assert report["generation_state"] == "AI_UNAVAILABLE"
        replay = await client.post("/api/v1/reports", json=h.body(), headers=headers)
        assert replay.json()["report"]["id"] == report["report"]["id"]
        assert replay.headers["Idempotency-Replayed"] == "true"
        conflict = await client.post(
            "/api/v1/reports", json={**h.body(), "locale": "vi"}, headers=headers
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
        await h.sql(
            "UPDATE tasks SET status='DONE',version=version+1 WHERE id=:id", {"id": h.task_id}
        )
        fetched = await client.get(f"/api/v1/reports/{report['report']['id']}")
        assert fetched.json()["snapshot"] == report["snapshot"]
        listed = await client.get(f"/api/v1/reports?project_id={h.project_id}")
        assert listed.json()["total"] == 1
        assert listed.headers["Cache-Control"] == "private, no-store"
    audits = await h.sql(
        "SELECT outcome::text FROM audit_events WHERE organization_id=:org "
        "AND action='report.created'",
        {"org": h.actor.organization_id},
    )
    assert sorted(row[0] for row in audits) == ["REJECTED", "SUCCEEDED"]


@pytest.mark.asyncio
async def test_manager_daily_metrics_without_provider_and_current_authority(
    report_harness: ReportHarness,
):
    h = report_harness
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        created = await client.post(
            "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
        )
        assert created.status_code == 201, created.text
        rid = created.json()["report"]["id"]
    for actor in (h.employee, h.foreign):
        async with AsyncClient(
            transport=ASGITransport(app=h.app(actor)), base_url="http://test"
        ) as client:
            read = await client.get(f"/api/v1/reports/{rid}")
            assert read.status_code == (403 if actor == h.employee else 404)
            denied = await client.post(
                "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
            )
            assert denied.status_code == (403 if actor == h.employee else 404)
    await h.sql(
        "UPDATE memberships SET role='EMPLOYEE' WHERE id=:id", {"id": h.actor.membership_id}
    )
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        assert (await client.get(f"/api/v1/reports/{rid}")).status_code == 403


@pytest.mark.asyncio
async def test_empty_and_reported_100_are_not_done(report_harness: ReportHarness):
    h = report_harness
    draft_id, update_id = uuid4(), uuid4()
    await h.sql(
        "INSERT INTO daily_update_drafts(id,organization_id,owner_membership_id,"
        "version) VALUES (:id,:org,:member,1)",
        {"id": draft_id, "org": h.actor.organization_id, "member": h.actor.membership_id},
    )
    await h.sql(
        "INSERT INTO daily_updates(id,organization_id,draft_id,owner_membership_id,"
        "draft_version,confirmed_at) VALUES (:id,:org,:draft,:member,1,now())",
        {
            "id": update_id,
            "org": h.actor.organization_id,
            "draft": draft_id,
            "member": h.actor.membership_id,
        },
    )
    await h.sql(
        "INSERT INTO task_progress_observations(id,organization_id,update_id,task_id,"
        "owner_membership_id,progress_version,reporting_date,reporting_at,confirmed_at,"
        "reported_percent,payload) VALUES (:id,:org,:update,:task,:member,1,current_date,"
        "now(),now(),100,'{}')",
        {
            "id": uuid4(),
            "org": h.actor.organization_id,
            "update": update_id,
            "task": h.task_id,
            "member": h.actor.membership_id,
        },
    )
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        observed = await client.post(
            "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
        )
        assert observed.status_code == 201, observed.text
        metrics = observed.json()["snapshot"]["metrics"]
        assert metrics["tasks.status.done_count"]["value"] == "0"
        assert metrics["tasks.status.in_progress_count"]["value"] == "1"
    h.project_id = uuid4()
    await h.sql(
        "INSERT INTO projects(id,organization_id,name,created_by_membership_id,"
        "updated_by_membership_id) VALUES (:id,:org,'Empty report project',:member,:member)",
        {"id": h.project_id, "org": h.actor.organization_id, "member": h.actor.membership_id},
    )
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        result = await client.post(
            "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
        )
        assert result.status_code == 201, result.text
        metrics = result.json()["snapshot"]["metrics"]
        assert metrics["tasks.status.total_count"]["value"] == "0"
        assert metrics["progress.observation_coverage"]["value"] is None
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
        future = await client.post(
            "/api/v1/reports",
            json={**h.body(), "period_start": tomorrow},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert future.status_code == 422


@pytest.mark.asyncio
async def test_capture_has_one_consistent_tenant_scope(report_harness: ReportHarness):
    h = report_harness
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        responses = await asyncio.gather(
            *[
                client.post(
                    "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
                )
                for _ in range(2)
            ]
        )
    for response in responses:
        assert response.status_code == 201, response.text
        snapshot = response.json()["snapshot"]
        counts = [
            snapshot["metrics"][f"tasks.status.{key}_count"]["value"]
            for key in ["to_do", "in_progress", "done"]
        ]
        assert sum(int(value) for value in counts) == int(
            snapshot["metrics"]["tasks.status.total_count"]["value"]
        )
        assert snapshot["receipts"][0]["isolation"] == "repeatable read"
        assert len(snapshot["sources"]) == 1


@pytest.mark.asyncio
async def test_report_defaults_show_actual_authorized_timezone(report_harness: ReportHarness):
    h = report_harness
    h.service = ReportService(
        ReportTransactions(async_sessionmaker(h.engine, expire_on_commit=False), "Asia/Ho_Chi_Minh")
    )
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        response = await client.get(f"/api/v1/reports/defaults?project_id={h.project_id}")
        assert response.status_code == 200, response.text
        assert response.json()["timezone"] == "Asia/Ho_Chi_Minh"
        assert (
            response.json()["period_start"]
            == datetime.now(UTC).astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).date().isoformat()
        )
        assert response.headers["Cache-Control"] == "private, no-store"
    async with AsyncClient(
        transport=ASGITransport(app=h.app(h.employee)), base_url="http://test"
    ) as client:
        assert (
            await client.get(f"/api/v1/reports/defaults?project_id={h.project_id}")
        ).status_code == 403


@pytest.mark.asyncio
async def test_repeatable_capture_uses_state_before_concurrent_commit(
    report_harness: ReportHarness, monkeypatch: pytest.MonkeyPatch
):
    from app.modules.reporting.adapters.repository import SQLReportRepository

    h = report_harness
    authorize = SQLReportRepository.authorize_project
    updated = False

    async def interleaved(repo: SQLReportRepository, project_id: UUID) -> None:
        nonlocal updated
        await authorize(repo, project_id)
        if not updated:
            updated = True
            await h.sql(
                "UPDATE tasks SET status='DONE',version=version+1 WHERE id=:id", {"id": h.task_id}
            )

    monkeypatch.setattr(SQLReportRepository, "authorize_project", interleaved)
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        result = await client.post(
            "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
        )
        assert result.status_code == 201, result.text
        snapshot = result.json()["snapshot"]
        assert snapshot["metrics"]["tasks.status.in_progress_count"]["value"] == "1"
        assert snapshot["metrics"]["tasks.status.done_count"]["value"] == "0"
        assert snapshot["sources"][0]["version"] == 1
    assert (
        await h.sql("SELECT status::text FROM tasks WHERE id=:id", {"id": h.task_id})
    ).scalar_one() == "DONE"


@pytest.mark.asyncio
async def test_concurrent_replay_and_rollback_leave_one_fact_set(
    report_harness: ReportHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.modules.reporting.adapters.repository import SQLReportRepository
    from app.modules.reporting.domain.reports import ReportError

    h = report_harness
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        key = str(uuid4())
        results = await asyncio.gather(
            *[
                client.post("/api/v1/reports", json=h.body(), headers={"Idempotency-Key": key})
                for _ in range(2)
            ]
        )
        assert [r.status_code for r in results] == [201, 201]
        assert results[0].json()["report"]["id"] == results[1].json()["report"]["id"]
        save = SQLReportRepository.save

        async def rejected_after_flush(repo: SQLReportRepository, *args: Any, **kwargs: Any):
            await save(repo, *args, **kwargs)
            raise ReportError("TEST_ROLLBACK")

        monkeypatch.setattr(SQLReportRepository, "save", rejected_after_flush)
        failed = await client.post(
            "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
        )
        assert failed.status_code == 409
    for table in (
        "reports",
        "report_metric_snapshots",
        "report_versions",
        "report_snapshot_sources",
        "report_snapshot_receipts",
    ):
        assert (
            await h.sql(
                f"SELECT count(*) FROM {table} WHERE organization_id=:org",
                {"org": h.actor.organization_id},
            )
        ).scalar_one() == 1
    assert (
        await h.sql(
            "SELECT count(*) FROM outbox_events WHERE organization_id=:org "
            "AND event_type='report.metrics_captured.v1'",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 1
    assert (
        await h.sql(
            "SELECT count(*) FROM audit_events WHERE organization_id=:org AND outcome='REJECTED'",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 1
