"""Weekly report SQL uses full scope and keeps historical activity separate."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Never
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from tests.test_report_api_integration import ReportHarness, pytestmark, report_harness

__all__ = ["pytestmark", "report_harness"]


@pytest.mark.asyncio
async def test_weekly_full_totals_and_historical_capture(report_harness: ReportHarness):
    h = report_harness
    today = datetime.now(UTC).date()
    last_monday = today - timedelta(days=today.weekday() + 7)
    for index in range(124):
        await h.sql(
            "INSERT INTO tasks(id,organization_id,project_id,title,status,required_skill_labels,"
            "created_by_membership_id,updated_by_membership_id) "
            "VALUES (:id,:org,:project,:title,'TO_DO','[]',:member,:member)",
            {
                "id": uuid4(),
                "org": h.actor.organization_id,
                "project": h.project_id,
                "title": f"Scope task {index}",
                "member": h.actor.membership_id,
            },
        )
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/reports",
            json={**h.body(), "kind": "WEEKLY", "period_start": last_monday.isoformat()},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 201, response.text
        snapshot = response.json()["snapshot"]
        assert snapshot["metrics"]["tasks.status.total_count"]["value"] == "125"
        assert snapshot["metrics"]["tasks.activity.created_count"]["value"] == "0"
        assert snapshot["metrics"]["tasks.status.total_count"]["time_basis"] == "AT_CAPTURE"
        assert snapshot["period"]["local_end"] == (last_monday + timedelta(days=7)).isoformat()
        assert len(snapshot["sources"]) == 125
        assert snapshot["receipts"][0]["row_count"] == 125
        assert snapshot["metrics"]["remaining.total_hours"]["value"] is None


@pytest.mark.asyncio
async def test_catalog_tracks_blockers_and_unavailable_risk(report_harness: ReportHarness):
    h = report_harness
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
        )
        assert response.status_code == 201
        metrics = response.json()["snapshot"]["metrics"]
        assert metrics["blockers.open_count"]["value"] == "0"
        assert metrics["risk.missing_count"]["value"] == "1"
        assert metrics["reporter.coverage"]["state"] == "NOT_APPLICABLE"
        assert metrics["workload.capacity_hours"]["value"] is None
        assert metrics["tasks.activity.done_transition_count"]["value"] is None


@pytest.mark.asyncio
async def test_week_baselines_use_phase4_semantics(report_harness: ReportHarness):
    h = report_harness
    monday = datetime.now(UTC).date() - timedelta(days=datetime.now(UTC).weekday())
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        week_response = await client.post(
            f"/api/v1/projects/{h.project_id}/weeks",
            json={
                "week_number": 1,
                "start_date": monday.isoformat(),
                "end_date": (monday + timedelta(days=4)).isoformat(),
                "objective": "Delivery",
            },
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert week_response.status_code == 201, week_response.text
        week = week_response.json()
        edited = await client.patch(
            f"/api/v1/tasks/{h.task_id}",
            json={"project_week_id": week["id"]},
            headers={"Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
        )
        assert edited.status_code == 200, edited.text
        response = await client.post(
            "/api/v1/reports",
            json={**h.body(), "kind": "WEEKLY", "period_start": monday.isoformat()},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 201, response.text
        metrics = response.json()["snapshot"]["metrics"]
        assert metrics[f"weekly.{week['id']}.current.reported_percent"]["value"] is None
        assert metrics[f"weekly.{week['id']}.current.total_effort_hours"]["value"] == "4"
        assert (
            metrics[f"weekly.{week['id']}.current.total_effort_hours"]["policy_version"]
            == "mon-fri-utc-v1"
        )
        assert metrics[f"weekly.{week['id']}.scope.added_count"]["value"] == "1"


@pytest.mark.asyncio
async def test_workload_reuses_capacity_leave_and_missing_is_unknown(report_harness: ReportHarness):
    h = report_harness
    monday = datetime.now(UTC).date() - timedelta(days=datetime.now(UTC).weekday())
    capacity_id, leave_id = uuid4(), uuid4()
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        week_response = await client.post(
            f"/api/v1/projects/{h.project_id}/weeks",
            json={
                "week_number": 1,
                "start_date": monday.isoformat(),
                "end_date": (monday + timedelta(days=4)).isoformat(),
                "objective": "Capacity",
            },
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert week_response.status_code == 201, week_response.text
        week_id = week_response.json()["id"]
        await h.sql(
            "UPDATE tasks SET project_week_id=:week,assignee_membership_id=:member WHERE id=:id",
            {"week": week_id, "member": h.employee.membership_id, "id": h.task_id},
        )
        body = {**h.body(), "kind": "WEEKLY", "period_start": monday.isoformat()}
        missing = await client.post(
            "/api/v1/reports", json=body, headers={"Idempotency-Key": str(uuid4())}
        )
        assert missing.status_code == 201, missing.text
        key = f"workload.{week_id}.{h.employee.membership_id}"
        assert missing.json()["snapshot"]["metrics"][f"{key}.capacity_hours"]["value"] is None
        await h.sql(
            "INSERT INTO "
            "capacity_entries(id,organization_id,membership_id,kind,hours,"
            "effective_from,effective_to) "
            "VALUES (:id,:org,:member,'DEFAULT',24,:start,:end)",
            {
                "id": capacity_id,
                "org": h.actor.organization_id,
                "member": h.employee.membership_id,
                "start": monday,
                "end": monday + timedelta(days=6),
            },
        )
        await h.sql(
            "INSERT INTO "
            "leave_entries(id,organization_id,membership_id,start_date,end_date,unavailable_hours) "
            "VALUES (:id,:org,:member,:start,:end,4)",
            {
                "id": leave_id,
                "org": h.actor.organization_id,
                "member": h.employee.membership_id,
                "start": monday,
                "end": monday + timedelta(days=4),
            },
        )
        made = await client.post(
            "/api/v1/reports", json=body, headers={"Idempotency-Key": str(uuid4())}
        )
        assert made.status_code == 201, made.text
        snap = made.json()["snapshot"]
        assert snap["metrics"][f"{key}.capacity_hours"]["value"] == "20"
        assert snap["metrics"][f"{key}.allocated_hours"]["value"] == "4"
        assert snap["metrics"][f"{key}.ratio"]["value"] == "0.2"
        assert {str(capacity_id), str(leave_id)} <= {s["resource_id"] for s in snap["sources"]}
        sources = await client.get(
            f"/api/v1/reports/{made.json()['report']['id']}/sources?page_size=100"
        )
        assert all(item["freshness"] == "CURRENT" for item in sources.json()["items"])
        await h.sql(
            "UPDATE capacity_entries SET hours=40,version=version+1 WHERE id=:id",
            {"id": capacity_id},
        )
        changed = await client.get(
            f"/api/v1/reports/{made.json()['report']['id']}/sources?page_size=100"
        )
        assert (
            next(
                x for x in changed.json()["items"] if x["source"]["resource_id"] == str(capacity_id)
            )["freshness"]
            == "UPDATED"
        )
        old = await client.get(f"/api/v1/reports/{made.json()['report']['id']}")
        assert old.json()["snapshot"]["snapshot_hash"] == snap["snapshot_hash"]
        assert old.json()["snapshot"]["metrics"][f"{key}.capacity_hours"]["value"] == "20"
        await h.sql(
            "UPDATE memberships SET is_active=false WHERE id=:id", {"id": h.employee.membership_id}
        )
        inactive = await client.post(
            "/api/v1/reports", json=body, headers={"Idempotency-Key": str(uuid4())}
        )
        assert inactive.status_code == 201, inactive.text
        assert inactive.json()["snapshot"]["metrics"][f"{key}.capacity_hours"]["state"] == "UNKNOWN"
        assert (
            inactive.json()["snapshot"]["metrics"]["workload.capacity_unknown_count"]["value"]
            == "1"
        )


@pytest.mark.asyncio
async def test_confirmed_correction_captures_effective_hours_and_stale_actuals(
    report_harness: ReportHarness,
):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.modules.progress.adapters.daily_update_repository import (
        SqlAlchemyDailyUpdateTransactions,
    )
    from app.modules.progress.application.daily_update_service import DailyUpdateService
    from tests.test_daily_update_api_integration import body, confirm, draft

    h = report_harness
    await h.sql(
        "UPDATE tasks SET assignee_membership_id=:member WHERE id=:id",
        {"member": h.actor.membership_id, "id": h.task_id},
    )
    app = h.app()
    app.state.daily_update_service = DailyUpdateService(
        SqlAlchemyDailyUpdateTransactions(
            async_sessionmaker(h.engine, class_=AsyncSession, expire_on_commit=False)
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await confirm(
            client,
            await draft(
                client,
                [
                    body(
                        str(h.task_id),
                        spent_hours="2",
                        reported_percent="50",
                        remaining_hours="14.5",
                    )
                ],
            ),
        )
        assert first.status_code == 201, first.text
        observation = first.json()["observations"][0]
        corrected = await confirm(
            client,
            await draft(
                client,
                [
                    body(
                        str(h.task_id),
                        spent_hours="3",
                        reported_percent="60",
                        remaining_hours="12",
                        expected_progress_version=1,
                        corrects_observation_id=observation["id"],
                        correction_reason="Corrected hours",
                    )
                ],
            ),
        )
        assert corrected.status_code == 201, corrected.text
        made = await client.post(
            "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
        )
        assert made.status_code == 201, made.text
        snapshot = made.json()["snapshot"]
        assert Decimal(snapshot["metrics"]["work_logs.effective_hours"]["value"]) == Decimal("3.00")
        assert Decimal(snapshot["metrics"]["remaining.total_hours"]["value"]) == Decimal("12.00")
        assert Decimal(snapshot["metrics"]["progress.reported_percent"]["value"]) == Decimal(
            "60.00"
        )
        assert snapshot["metrics"]["progress.observation_coverage"]["value"] == "1"
        assert snapshot["metrics"]["tasks.status.done_count"]["value"] == "0"
        assert len([s for s in snapshot["sources"] if s["resource_type"] == "WORK_LOG"]) == 2
        assert all(s["resource_type"] != "EVIDENCE" for s in snapshot["sources"])
        await h.sql(
            "UPDATE tasks SET assignee_membership_id=:member,version=version+1 WHERE id=:id",
            {"member": h.employee.membership_id, "id": h.task_id},
        )
        stale = await client.post(
            "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
        )
        assert stale.status_code == 201, stale.text
        assert stale.json()["snapshot"]["metrics"]["remaining.total_hours"]["value"] is None
        assert stale.json()["snapshot"]["metrics"]["remaining.stale_count"]["value"] == "1"
        assert stale.json()["snapshot"]["metrics"]["progress.observation_coverage"]["value"] == "0"
        assert (
            Decimal(stale.json()["snapshot"]["metrics"]["work_logs.effective_hours"]["value"]) == 3
        )


@pytest.mark.asyncio
async def test_stored_ai_risk_and_blocker_are_cited_without_rescoring(
    report_harness: ReportHarness,
):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.modules.progress.adapters.blocker_repository import SqlAlchemyBlockerTransactions
    from app.modules.progress.application.blocker_service import BlockerService
    from app.modules.progress.domain.blockers import BlockerCommand
    from app.modules.risk.adapters.repository import RiskTransactions
    from app.modules.risk.application.risk_service import RiskService
    from tests.test_risk_api_integration import Model

    h = report_harness
    sessions = async_sessionmaker(h.engine, class_=AsyncSession, expire_on_commit=False)
    await BlockerService(SqlAlchemyBlockerTransactions(sessions)).apply(
        h.actor,
        BlockerCommand(
            task_id=h.task_id,
            expected_task_version=1,
            action="CREATE",
            severity="HIGH",
            text="Awaiting materials",
        ),
        str(uuid4()),
    )
    risk = RiskService(RiskTransactions(sessions), Model())
    await risk.refresh(h.actor, h.task_id, uuid4())
    assert await risk.run_once(h.actor)
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        made = await client.post(
            "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
        )
        assert made.status_code == 201, made.text
        snap = made.json()["snapshot"]
        assert snap["metrics"]["blockers.open_count"]["value"] == "1"
        assert snap["metrics"]["blockers.severe_count"]["value"] == "1"
        assert snap["metrics"]["risk.ready_count"]["value"] == "1"
        source = next(s for s in snap["sources"] if s["resource_type"] == "RISK_ASSESSMENT")
        assert source["facts"]["score"] == "83"
        assert source["facts"]["model_ref"] == "mock-test"
        assert source["facts"]["prompt_version"]
        assert source["facts"]["policy_version"]
        assert snap["metrics"][f"risk.{source['resource_id']}.score"]["value"] == "83"
        sources = await client.get(f"/api/v1/reports/{made.json()['report']['id']}/sources")
        assert all(s["freshness"] == "CURRENT" for s in sources.json()["items"])


@pytest.mark.asyncio
async def test_reporting_window_uses_exact_roster_zero_denominator(report_harness: ReportHarness):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.modules.automations.adapters.repository import ScheduleTransactions
    from app.modules.automations.application.schedule_service import ScheduleService
    from app.modules.automations.domain.schedules import ScheduleCommand

    h = report_harness
    service = ScheduleService(
        ScheduleTransactions(
            async_sessionmaker(h.engine, class_=AsyncSession, expire_on_commit=False)
        )
    )
    command = ScheduleCommand(
        project_id=h.project_id,
        timezone="UTC",
        weekdays=(1, 2, 3, 4, 5, 6, 7),
        cutoff="17:00",
        recipients=(h.actor.membership_id,),
    )
    preview = await service.preview(h.actor, command, 0, str(uuid4()))
    await service.confirm(h.actor, preview.id, 0, str(uuid4()))
    view = await service.get(h.actor, h.project_id)
    assert view.window is not None
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        made = await client.post(
            "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
        )
        assert made.status_code == 201, made.text
        snap = made.json()["snapshot"]
        assert snap["metrics"]["reporter.coverage"]["value"] is None
        assert snap["metrics"]["reporter.coverage"]["state"] == "NOT_APPLICABLE"
        source = next(s for s in snap["sources"] if s["resource_type"] == "REPORTING_WINDOW")
        assert source["facts"]["expected_count"] == 0
        assert source["facts"]["coverage_state"] == "NO_REPORTERS"
        # Seed an exact frozen roster fact to exercise coverage independently of window creation.
        await h.sql(
            "INSERT INTO reporting_window_reporters(organization_id,window_id,membership_id) "
            "VALUES (:org,:window,:member)",
            {
                "org": h.actor.organization_id,
                "window": view.window.id,
                "member": h.actor.membership_id,
            },
        )
        await h.sql(
            "UPDATE tasks SET assignee_membership_id=:member WHERE id=:id",
            {"member": h.actor.membership_id, "id": h.task_id},
        )
        pending = await client.post(
            "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
        )
        assert pending.status_code == 201, pending.text
        before = pending.json()["snapshot"]
        assert before["metrics"]["reporter.coverage"]["value"] == "0"
        from app.modules.progress.adapters.daily_update_repository import (
            SqlAlchemyDailyUpdateTransactions,
        )
        from app.modules.progress.application.daily_update_service import DailyUpdateService
        from tests.test_daily_update_api_integration import body, confirm, draft

        app = h.app()
        app.state.daily_update_service = DailyUpdateService(
            SqlAlchemyDailyUpdateTransactions(
                async_sessionmaker(h.engine, class_=AsyncSession, expire_on_commit=False)
            )
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as owner:
            confirmed = await confirm(owner, await draft(owner, [body(str(h.task_id))]))
            assert confirmed.status_code == 201, confirmed.text
        complete = await client.post(
            "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
        )
        assert complete.status_code == 201, complete.text
        after = complete.json()["snapshot"]
        assert after["metrics"]["reporter.coverage"]["value"] == "1"
        assert after["metrics"]["progress.observation_coverage"]["value"] == "1"
        window_source = next(
            s for s in after["sources"] if s["resource_type"] == "REPORTING_WINDOW"
        )
        assert window_source["facts"]["expected_count"] == 1
        assert window_source["facts"]["reported_count"] == 1
        changed = await client.get(f"/api/v1/reports/{pending.json()['report']['id']}/sources")
        assert (
            next(
                item
                for item in changed.json()["items"]
                if item["source"]["resource_type"] == "REPORTING_WINDOW"
            )["freshness"]
            == "UPDATED"
        )
        old = await client.get(f"/api/v1/reports/{pending.json()['report']['id']}")
        assert old.json()["snapshot"]["snapshot_hash"] == before["snapshot_hash"]


@pytest.mark.asyncio
async def test_weekly_missing_estimate_does_not_become_known_zero(report_harness: ReportHarness):
    h = report_harness
    monday = datetime.now(UTC).date() - timedelta(days=datetime.now(UTC).weekday())
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        week = await client.post(
            f"/api/v1/projects/{h.project_id}/weeks",
            json={
                "week_number": 1,
                "start_date": monday.isoformat(),
                "end_date": (monday + timedelta(days=4)).isoformat(),
                "objective": "Unknown estimate",
            },
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert week.status_code == 201
        edit = await client.patch(
            f"/api/v1/tasks/{h.task_id}",
            json={"project_week_id": week.json()["id"]},
            headers={"Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
        )
        assert edit.status_code == 200, edit.text
        # A migrated/imported legacy baseline may lack effort; report must preserve unknown.
        await h.sql(
            "INSERT INTO weekly_plan_baselines(id,organization_id,project_week_id,sequence,"
            "kind,captured_at,payload) "
            "SELECT :id,organization_id,project_week_id,sequence+1,'MANUAL',now(),"
            "jsonb_set(payload, '{task_entries,0,effort_hours}', 'null'::jsonb) "
            "FROM weekly_plan_baselines WHERE organization_id=:org AND project_week_id=:week "
            "ORDER BY sequence DESC LIMIT 1",
            {"id": uuid4(), "org": h.actor.organization_id, "week": week.json()["id"]},
        )
        made = await client.post(
            "/api/v1/reports",
            json={**h.body(), "kind": "WEEKLY", "period_start": monday.isoformat()},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert made.status_code == 201, made.text
        snap = made.json()["snapshot"]
        key = f"weekly.{week.json()['id']}.current"
        assert snap["metrics"][f"{key}.total_effort_hours"]["value"] is None
        assert snap["metrics"][f"{key}.estimated_effort_known_subtotal_hours"]["value"] == "0"
        assert snap["metrics"][f"{key}.missing_estimate_count"]["value"] == "1"
        source_set = {
            (s["resource_type"], s["resource_id"], s["fingerprint"]) for s in snap["sources"]
        }
        assert all(
            (ref["resource_type"], ref["resource_id"], ref["fingerprint"]) in source_set
            for m in snap["metrics"].values()
            for ref in m["source_refs"]
        )


@pytest.mark.asyncio
async def test_capture_remains_consistent_across_concurrent_task_change(
    report_harness: ReportHarness,
):
    from app.modules.reporting.domain.commands import CaptureReportCommand
    from app.modules.reporting.domain.periods import ReportKind, normalize_period

    h = report_harness
    async with h.service.transactions(h.actor) as repo:
        await repo.authenticate()
        await repo.authorize_project(h.project_id)
        at = await repo.captured_at()
        await h.sql(
            "UPDATE tasks SET status='DONE',version=version+1 WHERE id=:id", {"id": h.task_id}
        )
        snapshot = await repo.snapshot_reader.capture(
            CaptureReportCommand(
                report_id=uuid4(),
                snapshot_id=uuid4(),
                project_id=h.project_id,
                captured_at=at,
                period=normalize_period(ReportKind.DAILY, at.date(), "UTC", at),
            )
        )
        assert snapshot.metrics["tasks.status.in_progress_count"].value == 1
        assert snapshot.metrics["tasks.status.done_count"].value == 0
        assert next(s for s in snapshot.sources if s.resource_id == h.task_id).version == 1
    assert (
        await h.sql("SELECT version FROM tasks WHERE id=:id", {"id": h.task_id})
    ).scalar_one() == 2


@pytest.mark.asyncio
async def test_query_timeout_rolls_back_retries_and_audits_safe_failure(
    report_harness: ReportHarness, monkeypatch: pytest.MonkeyPatch
):
    from sqlalchemy import text

    from app.modules.reporting.adapters.snapshot_reader import SQLReportSnapshotReader
    from app.modules.reporting.domain.commands import CaptureReportCommand

    h = report_harness
    calls: list[UUID] = []

    async def timeout(reader: SQLReportSnapshotReader, command: CaptureReportCommand) -> Never:
        calls.append(command.report_id)
        await reader.session.execute(text("SET LOCAL statement_timeout='1ms'"))
        await reader.session.execute(text("SELECT pg_sleep(0.1)"))
        raise AssertionError("timeout did not fire")

    monkeypatch.setattr(SQLReportSnapshotReader, "capture", timeout)
    async with AsyncClient(
        transport=ASGITransport(app=h.app(), raise_app_exceptions=False), base_url="http://test"
    ) as client:
        result = await client.post(
            "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
        )
        assert result.status_code == 409, result.text
        assert result.json()["error"]["code"] == "REPORT_CAPTURE_RETRY"
    assert len(calls) == 3
    assert (
        await h.sql(
            "SELECT count(*) FROM reports WHERE organization_id=:org",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 0
    assert (
        await h.sql(
            "SELECT count(*) FROM audit_events WHERE organization_id=:org AND "
            "action='report.created' AND outcome='REJECTED'",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 1


@pytest.mark.asyncio
async def test_sealed_week_preserves_phase4_evaluation_instant(report_harness: ReportHarness):
    h = report_harness
    monday = datetime.now(UTC).date() - timedelta(days=datetime.now(UTC).weekday() + 7)
    sealed = datetime.combine(monday, datetime.min.time(), UTC) + timedelta(hours=12)
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        week = await client.post(
            f"/api/v1/projects/{h.project_id}/weeks",
            json={
                "week_number": 1,
                "start_date": monday.isoformat(),
                "end_date": (monday + timedelta(days=4)).isoformat(),
                "objective": "Imported closed week",
            },
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert week.status_code == 201
        edited = await client.patch(
            f"/api/v1/tasks/{h.task_id}",
            json={"project_week_id": week.json()["id"]},
            headers={"Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
        )
        assert edited.status_code == 200, edited.text
        # Append a historical sealed baseline fixture; no immutable baseline is rewritten.
        await h.sql(
            "INSERT INTO weekly_plan_baselines(id,organization_id,project_week_id,sequence,"
            "kind,captured_at,sealed_at,payload) SELECT :id,organization_id,project_week_id,"
            "sequence+1,'MANUAL',:sealed,:sealed,payload FROM weekly_plan_baselines WHERE "
            "organization_id=:org AND project_week_id=:week ORDER BY sequence DESC LIMIT 1",
            {
                "id": uuid4(),
                "sealed": sealed,
                "org": h.actor.organization_id,
                "week": week.json()["id"],
            },
        )
        made = await client.post(
            "/api/v1/reports",
            json={**h.body(), "kind": "WEEKLY", "period_start": monday.isoformat()},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert made.status_code == 201, made.text
        assert (
            made.json()["snapshot"]["metrics"][
                f"weekly.{week.json()['id']}.current.planned_percent"
            ]["value"]
            == "20"
        )
