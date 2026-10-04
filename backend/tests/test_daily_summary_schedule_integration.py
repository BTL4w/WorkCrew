"""Confirmed schedule configuration, frozen windows and current-actor permissions."""

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.automations.adapters.repository import ScheduleTransactions
from app.modules.automations.application.schedule_service import ScheduleService
from app.modules.automations.domain.schedules import ScheduleCommand, ScheduleError
from app.modules.organization.domain.roles import MembershipRole
from tests.test_daily_update_api_integration import seed_task
from tests.test_evidence_api_integration import Harness, harness

__all__ = ["harness"]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


async def prepared(h: Harness):
    task = UUID(await seed_task(h))
    project = (await h.sql("SELECT project_id FROM tasks WHERE id=:id", {"id": task})).scalar_one()
    await h.sql("UPDATE memberships SET role='MANAGER' WHERE id=:id", {"id": h.peer.membership_id})
    manager = replace(h.peer, role=MembershipRole.MANAGER)
    sessions = async_sessionmaker(
        bind=h.connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    now = [datetime.now(UTC)]
    service = ScheduleService(ScheduleTransactions(sessions), clock=lambda: now[0])
    command = ScheduleCommand(
        project_id=project,
        timezone="Asia/Ho_Chi_Minh",
        weekdays=(1, 2, 3, 4, 5, 6, 7),
        cutoff="17:00",
        recipients=(manager.membership_id,),
    )
    return task, manager, command, service, now


@pytest.mark.asyncio
async def test_empty_roster_and_schedule_edit_preserve_window(harness: Harness):
    task, actor, command, service, now = await prepared(harness)
    await harness.sql("UPDATE tasks SET assignee_membership_id=NULL WHERE id=:id", {"id": task})
    now[0] = datetime.now(UTC)
    draft = await service.preview(actor, command, 0, str(uuid4()))
    assert (await service.get(actor, command.project_id)).schedule is None
    schedule = await service.confirm(actor, draft.id, 0, str(uuid4()))
    original = await service.get(actor, command.project_id)
    window = original.window
    assert (
        window is not None and window.coverage_state == "NO_REPORTERS" and not window.full_coverage
    )
    draft = await service.preview(
        actor, command.model_copy(update={"cutoff": "18:00", "timezone": "UTC"}), 1, str(uuid4())
    )
    edited = await service.confirm(actor, draft.id, 1, str(uuid4()))
    current = await service.get(actor, command.project_id)
    assert current.window is not None
    assert current.window.id == window.id and current.window.cutoff_at == window.cutoff_at
    assert current.window.applied_version == 1
    now[0] = window.ends_at + timedelta(seconds=1)
    following = await service.get(actor, command.project_id)
    assert following.window is not None
    assert following.window.applied_version == edited.version == 2
    assert following.window.id != window.id
    assert schedule.id == edited.id


@pytest.mark.asyncio
async def test_confirmation_replay_stale_permissions_recipients_and_audit(harness: Harness):
    _, actor, command, service, _ = await prepared(harness)
    draft = await service.preview(actor, command, 0, str(uuid4()))
    key = str(uuid4())
    saved = await service.confirm(actor, draft.id, 0, key)
    assert await service.confirm(actor, draft.id, 0, key) == saved
    with pytest.raises(ScheduleError):
        await service.confirm(actor, draft.id, 0, str(uuid4()))
    with pytest.raises(ScheduleError):
        await service.preview(harness.actor, command, 1, str(uuid4()))
    with pytest.raises(ScheduleError):
        await service.get(harness.foreign, command.project_id)
    with pytest.raises(ScheduleError):
        await service.preview(
            actor,
            command.model_copy(update={"recipients": (harness.foreign.membership_id,)}),
            1,
            str(uuid4()),
        )
    await harness.sql(
        "UPDATE memberships SET is_active=false WHERE id=:id", {"id": actor.membership_id}
    )
    with pytest.raises(ScheduleError):
        await service.confirm(actor, draft.id, 0, key)
    assert (
        await harness.sql(
            "SELECT count(*) FROM audit_events WHERE organization_id=:org "
            "AND action LIKE 'schedule.%' AND outcome='REJECTED'",
            {"org": actor.organization_id},
        )
    ).scalar_one() >= 4


@pytest.mark.asyncio
async def test_roster_is_frozen_and_pause_takes_effect_immediately(harness: Harness):
    task, actor, command, service, _ = await prepared(harness)
    draft = await service.preview(actor, command, 0, str(uuid4()))
    schedule = await service.confirm(actor, draft.id, 0, str(uuid4()))
    first = await service.get(actor, command.project_id)
    assert first.window is not None
    assert first.window.expected_reporters == (harness.actor.membership_id,)
    await harness.sql(
        "UPDATE tasks SET assignee_membership_id=NULL,version=version+1 WHERE id=:id", {"id": task}
    )
    changed = await service.get(actor, command.project_id)
    assert changed.window is not None
    assert changed.window.expected_reporters == first.window.expected_reporters
    assert changed.window.scope_changed
    paused = await service.pause(actor, schedule.id, 1, True, str(uuid4()))
    assert paused.paused
    paused_view = await service.get(actor, command.project_id)
    assert paused_view.window is not None and not paused_view.window.enabled
    resumed = await service.pause(actor, schedule.id, paused.version, False, str(uuid4()))
    assert not resumed.paused


@pytest.mark.asyncio
async def test_late_window_open_uses_assignment_at_boundary(harness: Harness):
    task, actor, command, service, now = await prepared(harness)
    draft = await service.preview(actor, command, 0, str(uuid4()))
    await service.confirm(actor, draft.id, 0, str(uuid4()))
    first = await service.get(actor, command.project_id)
    assert first.window is not None
    boundary = first.window.ends_at
    # Business changes after opening must not alter an unmaterialized window's denominator.
    await harness.sql(
        "UPDATE tasks SET assignee_membership_id=NULL,version=version+1 WHERE id=:id",
        {"id": task},
    )
    # Historical fixture timestamps simulate a worker restart late in the following day.
    for assignee, timestamp in (
        (harness.actor.membership_id, boundary - timedelta(seconds=1)),
        (None, boundary + timedelta(hours=1)),
    ):
        await harness.sql(
            "INSERT INTO automation_task_scope_history "
            "(id,organization_id,task_id,assignee_membership_id,"
            "status,reporter_active,recorded_at) "
            "VALUES(:id,:org,:task,:member,'IN_PROGRESS',true,:at)",
            {
                "id": uuid4(),
                "org": actor.organization_id,
                "task": task,
                "member": assignee,
                "at": timestamp,
            },
        )
    now[0] = boundary + timedelta(hours=2)
    following = await service.get(actor, command.project_id)
    assert following.window is not None
    assert following.window.expected_reporters == (harness.actor.membership_id,)
    assert following.window.scope_changed


@pytest.mark.asyncio
async def test_schedule_http_schema_rejection_and_direct_rls(harness: Harness):
    from httpx import ASGITransport, AsyncClient

    _, actor, command, service, _ = await prepared(harness)
    app = harness.app(actor)
    app.state.schedule_service = service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        base = "/api/v1/automations/daily-summaries"
        response = await client.post(
            base + "/preview",
            json={"command": command.model_dump(mode="json"), "expected_version": 0},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 201, response.text
        draft = response.json()
        bad = await client.post(
            base + "/confirm", json={"draft_id": draft["id"], "expected_version": 0}
        )
        assert bad.status_code == 400
        confirmed = await client.post(
            base + "/confirm",
            json={"draft_id": draft["id"], "expected_version": 0},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert confirmed.status_code == 201, confirmed.text
        invalid = await client.post(
            base + "/preview",
            json={
                "command": {**command.model_dump(mode="json"), "timezone": "Invalid/Zone"},
                "expected_version": 1,
            },
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert invalid.status_code == 422
        view = await client.get(base, params={"project_id": str(command.project_id)})
        assert view.status_code == 200 and view.json()["window"]["applied_version"] == 1
    # Another tenant's Manager cannot read the rows even through direct SQL.
    await harness.sql(
        "UPDATE memberships SET role='MANAGER' WHERE id=:id", {"id": harness.foreign.membership_id}
    )
    from sqlalchemy import text

    await harness.connection.execute(text("SET LOCAL ROLE app_runtime"))
    await harness.connection.execute(
        text(
            "SELECT set_config('app.organization_id',:org,true),"
            "set_config('app.membership_id',:member,true)"
        ),
        {"org": str(harness.foreign.organization_id), "member": str(harness.foreign.membership_id)},
    )
    assert (
        await harness.connection.execute(text("SELECT count(*) FROM automation_schedules"))
    ).scalar_one() == 0
    assert (
        await harness.connection.execute(text("SELECT count(*) FROM reporting_windows"))
    ).scalar_one() == 0


@pytest.mark.asyncio
async def test_expired_draft_pending_preview_and_pause_replay(harness: Harness):
    _, actor, command, service, now = await prepared(harness)
    expired = await service.preview(actor, command, 0, str(uuid4()))
    now[0] = expired.expires_at + timedelta(seconds=1)
    with pytest.raises(ScheduleError, match="DRAFT_EXPIRED"):
        await service.confirm(actor, expired.id, 0, str(uuid4()))
    draft = await service.preview(actor, command, 0, str(uuid4()))
    saved = await service.confirm(actor, draft.id, 0, str(uuid4()))
    pending = await service.preview(
        actor, command.model_copy(update={"cutoff": "18:30"}), 1, str(uuid4())
    )
    view = await service.get(actor, command.project_id)
    assert view.window is not None
    now[0] = view.window.ends_at + timedelta(seconds=1)
    with pytest.raises(ScheduleError):
        await service.confirm(actor, pending.id, 1, str(uuid4()))
    key = str(uuid4())
    paused = await service.pause(actor, saved.id, 1, True, key)
    assert await service.pause(actor, saved.id, 1, True, key) == paused
    with pytest.raises(ScheduleError):
        await service.pause(actor, saved.id, 1, False, key)
