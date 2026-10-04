"""Real committed sessions exercise coverage/cutoff and delivery retry races."""

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.config import Settings
from app.core.database import create_database_engine, create_session_factory
from app.modules.automations.adapters.digest_repository import DigestRepository, DigestTransactions
from app.modules.automations.adapters.repository import ScheduleTransactions
from app.modules.automations.application.digest_service import DigestService
from app.modules.automations.application.schedule_service import ScheduleService
from app.modules.automations.domain.digests import AuthorizedJobScope
from app.modules.automations.domain.schedules import ScheduleCommand, ScheduleError
from app.modules.organization.domain.roles import MembershipRole
from app.modules.progress.domain.daily_updates import ConfirmDailyUpdateCommand
from tests.test_daily_update_concurrency_integration import Case, case, item

__all__ = ["case"]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


async def setup(case: Case, *, extra_recipient: bool = False, send_when_complete: bool = True):
    engine = create_database_engine(Settings(environment="test"))
    sessions = create_session_factory(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE memberships SET role='MANAGER' WHERE id=:id"),
            {"id": case.actor.membership_id},
        )
        project = (
            await conn.execute(
                text("SELECT project_id FROM tasks WHERE id=:id"), {"id": case.tasks[0]}
            )
        ).scalar_one()
    recipients = [case.actor.membership_id]
    if extra_recipient:
        user, member = uuid4(), uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users(id,email_normalized,email_display,"
                    "display_name,password_hash) "
                    "VALUES(:id,:email,:email,'Other reporter','unused')"
                ),
                {"id": user, "email": f"{user}@test.local"},
            )
            await conn.execute(
                text(
                    "INSERT INTO memberships(id,organization_id,user_id,role) "
                    "VALUES(:id,:org,:user,'EMPLOYEE')"
                ),
                {"id": member, "org": case.actor.organization_id, "user": user},
            )
            await conn.execute(
                text(
                    "UPDATE tasks SET project_id=:project,assignee_membership_id=:member "
                    "WHERE id=:task"
                ),
                {"project": project, "member": member, "task": case.tasks[1]},
            )
        recipients.append(member)
    actor = replace(case.actor, role=MembershipRole.MANAGER)
    schedules = ScheduleService(ScheduleTransactions(sessions))
    draft = await schedules.preview(
        actor,
        ScheduleCommand(
            project_id=project,
            timezone="UTC",
            weekdays=(1, 2, 3, 4, 5, 6, 7),
            cutoff="00:00",
            send_when_complete=send_when_complete,
            recipients=tuple(recipients),
        ),
        0,
        str(uuid4()),
    )
    saved = await schedules.confirm(actor, draft.id, 0, str(uuid4()))
    window = (await schedules.get(actor, project)).window
    assert window is not None
    return engine, actor, schedules, saved, window, DigestService(DigestTransactions(sessions))


@pytest.mark.asyncio
async def test_coverage_cutoff_race_delivers_once(case: Case):
    engine, actor, schedules, saved, window, digests = await setup(case)
    try:
        draft = await case.service.create_draft(
            actor, (item(case.tasks[0]),), str(uuid4()), "digest"
        )
        await case.service.confirm(
            actor,
            ConfirmDailyUpdateCommand(draft_id=draft.id, expected_draft_version=draft.version),
            str(uuid4()),
            "digest",
        )
        scope = AuthorizedJobScope(actor=actor, at=datetime.now(UTC))
        results = await asyncio.gather(
            *(
                digests.trigger(scope, window.id, reason)
                for reason in ("COVERAGE", "CUTOFF", "CUTOFF")
            )
        )
        assert len({result.id for result in results}) == 1
        await asyncio.gather(*(digests.deliver(scope, worker_id=f"worker-{i}") for i in range(2)))
        async with engine.connect() as conn:
            primary_trigger_count = await conn.scalar(
                text("SELECT count(*) FROM daily_summary_triggers WHERE organization_id=:org"),
                {"org": actor.organization_id},
            )
            summary_snapshot_count = await conn.scalar(
                text("SELECT count(*) FROM daily_summary_snapshots WHERE organization_id=:org"),
                {"org": actor.organization_id},
            )
            deliveries_per_authorized_recipient = await conn.scalar(
                text(
                    "SELECT count(*) FROM daily_summary_deliveries WHERE organization_id=:org "
                    "AND state='DELIVERED'"
                ),
                {"org": actor.organization_id},
            )
        assert primary_trigger_count == 1
        assert summary_snapshot_count == 1
        assert deliveries_per_authorized_recipient == 1
        assert len(await digests.list(actor)) == 1
        pause = await schedules.pause(actor, saved.id, saved.version, True, str(uuid4()))
        await schedules.pause(actor, saved.id, pause.version, False, str(uuid4()))
        assert (await digests.trigger(scope, window.id, "RESUME")).id == results[0].id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pause_revocation_and_empty_window(case: Case):
    engine, actor, schedules, saved, window, digests = await setup(case)
    try:
        paused = await schedules.pause(actor, saved.id, saved.version, True, str(uuid4()))
        scope = AuthorizedJobScope(actor=actor, at=datetime.now(UTC))
        with pytest.raises(ScheduleError, match="SCHEDULE_PAUSED"):
            await digests.trigger(scope, window.id, "CUTOFF")
        await schedules.pause(actor, saved.id, paused.version, False, str(uuid4()))
        await digests.trigger(scope, window.id, "CUTOFF")
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE memberships SET is_active=false WHERE id=:id"),
                {"id": actor.membership_id},
            )
        with pytest.raises(ScheduleError):
            await digests.deliver(scope, worker_id="revoked")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_restart_lease_fence_and_old_window_is_not_backfilled(case: Case):
    engine, actor, _, _, window, digests = await setup(case)
    try:
        scope = AuthorizedJobScope(actor=actor, at=datetime.now(UTC))
        await digests.trigger(scope, window.id, "CUTOFF")
        async with digests.transactions(actor) as repo:
            await repo.authenticate()
            claimed = await repo.claim(scope.at, "crashed")
        assert claimed is not None
        recovered = AuthorizedJobScope(actor=actor, at=scope.at + timedelta(seconds=61))
        assert await digests.deliver(recovered, worker_id="restarted")
        assert len(await digests.list(actor)) == 1
        with pytest.raises(ScheduleError, match="LEASE_LOST"):
            async with digests.transactions(actor) as repo:
                await repo.complete_delivery(claimed, recovered.at, "crashed")
        with pytest.raises(ScheduleError, match="WINDOW_NOT_ACTIVE"):
            await digests.trigger(
                AuthorizedJobScope(actor=actor, at=window.ends_at + timedelta(seconds=1)),
                window.id,
                "RESUME",
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_empty_roster_has_cutoff_only_and_snapshot_stays_immutable(case: Case):
    engine, actor, schedules, saved, _, digests = await setup(case)
    try:
        # A new reporting day opens after current assignment is removed.
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET assignee_membership_id=NULL WHERE id=:id"),
                {"id": case.tasks[0]},
            )
        first = (await schedules.get(actor, saved.project_id)).window
        assert first is not None
        at = first.ends_at + timedelta(hours=18)
        later = ScheduleService(schedules.transactions, clock=lambda: at)
        window = (await later.get(actor, saved.project_id)).window
        assert window is not None and window.coverage_state == "NO_REPORTERS"
        scope = AuthorizedJobScope(actor=actor, at=at)
        with pytest.raises(ScheduleError, match="TRIGGER_NOT_DUE"):
            await digests.trigger(scope, window.id, "COVERAGE")
        snapshot = await digests.trigger(scope, window.id, "CUTOFF")
        assert snapshot.expected_count == 0 and not snapshot.window.full_coverage
        await digests.deliver(scope, worker_id="empty")
        assert len(await digests.list(actor)) == 1
        from sqlalchemy.exc import DBAPIError

        with pytest.raises(DBAPIError):
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE daily_summary_snapshots SET payload='{}' WHERE id=:id"),
                    {"id": snapshot.id},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_employee_projection_and_current_permission_revocation(case: Case):
    engine, actor, _schedules, _saved, window, digests = await setup(case, extra_recipient=True)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT m.id,m.user_id,u.email_display FROM memberships m "
                        "JOIN users u ON u.id=m.user_id "
                        "WHERE m.organization_id=:org AND m.role='EMPLOYEE'"
                    ),
                    {"org": actor.organization_id},
                )
            ).one()
        employee = replace(
            actor,
            membership_id=row.id,
            user_id=row.user_id,
            email=row.email_display,
            role=MembershipRole.EMPLOYEE,
        )
        scope = AuthorizedJobScope(actor=actor, at=datetime.now(UTC))
        await digests.trigger(scope, window.id, "CUTOFF")
        await digests.deliver(scope, worker_id="manager")
        await digests.deliver(scope, worker_id="employee")
        manager_cards, employee_cards = await digests.list(actor), await digests.list(employee)
        assert len(manager_cards[0].snapshot.tasks) == 2
        own = employee_cards[0].snapshot
        assert own.scope == "OWN_WORK" and len(own.tasks) == 1
        assert own.tasks[0].id == case.tasks[1] and own.expected_count == 1
        assert actor.membership_id not in own.missing_reporters
        assert all(reporter.membership_id == employee.membership_id for reporter in own.reporters)
        with pytest.raises(ScheduleError):
            await digests.trigger(
                AuthorizedJobScope(actor=employee, at=scope.at), window.id, "CUTOFF"
            )
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET assignee_membership_id=NULL WHERE id=:id"),
                {"id": case.tasks[1]},
            )
        assert await digests.list(employee) == ()
        # Direct snapshot access remains Manager-only even for an eligible recipient.
        async with create_session_factory(engine)() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true),"
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(employee.membership_id)},
            )
            assert await session.scalar(text("SELECT count(*) FROM daily_summary_snapshots")) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_scheduler_uses_cutoff_when_coverage_delivery_disabled(case: Case):
    from app.modules.automations.adapters.scheduler import Scheduler
    from app.modules.identity.adapters.auth_repository import SqlAlchemyAuthTransactionFactory
    from app.modules.identity.adapters.current_actor import CurrentActorResolver
    from app.modules.identity.application.current_actor_service import CurrentActorService

    engine, actor, _, _, _, digests = await setup(case, send_when_complete=False)
    try:
        draft = await case.service.create_draft(
            actor, (item(case.tasks[0]),), str(uuid4()), "digest"
        )
        await case.service.confirm(
            actor,
            ConfirmDailyUpdateCommand(draft_id=draft.id, expected_draft_version=draft.version),
            str(uuid4()),
            "digest",
        )
        sessions = create_session_factory(engine)
        scheduler = Scheduler(
            sessions,
            CurrentActorResolver(CurrentActorService(SqlAlchemyAuthTransactionFactory(sessions))),
        )
        assert await scheduler.run_once(worker_id="cutoff", organization_id=actor.organization_id)
        feed = await digests.list(actor)
        assert len(feed) == 1 and feed[0].snapshot.reason == "CUTOFF"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_third_abandoned_lease_becomes_failed(case: Case):
    engine, actor, _, _, window, digests = await setup(case)
    try:
        at = datetime.now(UTC)
        await digests.trigger(AuthorizedJobScope(actor=actor, at=at), window.id, "CUTOFF")
        for attempt in range(3):
            async with digests.transactions(actor) as repo:
                await repo.authenticate()
                assert await repo.claim(at + timedelta(seconds=61 * attempt), f"crashed-{attempt}")
        assert not await digests.deliver(
            AuthorizedJobScope(actor=actor, at=at + timedelta(seconds=183)), worker_id="restarted"
        )
        async with engine.connect() as conn:
            assert (
                await conn.scalar(
                    text("SELECT state FROM daily_summary_deliveries WHERE organization_id=:org"),
                    {"org": actor.organization_id},
                )
                == "FAILED"
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("capture_before_edit", [True, False])
async def test_removed_schedule_recipient_is_immediately_revoked(
    case: Case, capture_before_edit: bool
):
    engine, actor, schedules, saved, window, digests = await setup(case, extra_recipient=True)
    try:
        scope = AuthorizedJobScope(actor=actor, at=datetime.now(UTC))
        if capture_before_edit:
            await digests.trigger(scope, window.id, "CUTOFF")
        command = ScheduleCommand(
            **{k: v for k, v in saved.model_dump().items() if k in ScheduleCommand.model_fields}
        ).model_copy(update={"recipients": (actor.membership_id,)})
        draft = await schedules.preview(actor, command, saved.version, str(uuid4()))
        await schedules.confirm(actor, draft.id, saved.version, str(uuid4()))
        snapshot = await digests.trigger(scope, window.id, "CUTOFF")
        assert snapshot.window.applied_version == 1
        for _ in range(3):
            await digests.deliver(scope, worker_id="recipient-revocation")
        async with engine.connect() as conn:
            delivered = tuple(
                (
                    await conn.execute(
                        text(
                            "SELECT recipient_id FROM daily_summary_deliveries "
                            "WHERE organization_id=:org AND state='DELIVERED'"
                        ),
                        {"org": actor.organization_id},
                    )
                ).scalars()
            )
        assert delivered == (actor.membership_id,)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_delivery_waits_for_authority_before_locking_job(case: Case):
    engine, actor, _, _, window, digests = await setup(case)
    try:
        at = datetime.now(UTC)
        await digests.trigger(AuthorizedJobScope(actor=actor, at=at), window.id, "CUTOFF")
        async with digests.transactions(actor) as repo:
            await repo.authenticate()
            delivery = await repo.claim(at, "lock-order")
        assert delivery is not None
        pid = asyncio.get_running_loop().create_future()

        async def complete():
            async with digests.transactions(actor) as repo:
                assert isinstance(repo, DigestRepository)
                pid.set_result(await repo.session.scalar(text("SELECT pg_backend_pid()")))
                await repo.complete_delivery(delivery, at, "lock-order")

        async with engine.connect() as holder:
            transaction = await holder.begin()
            await holder.execute(
                text("SELECT id FROM memberships WHERE id=:id FOR UPDATE"),
                {"id": actor.membership_id},
            )
            pending = asyncio.create_task(complete())
            try:
                backend_pid = await pid
                async with engine.connect() as probe:
                    blocked = False
                    for _ in range(100):
                        blocked = await probe.scalar(
                            text(
                                "SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"
                            ),
                            {"pid": backend_pid},
                        )
                        if blocked:
                            break
                        await asyncio.sleep(0.02)
                    assert blocked
                    # A claimant already holding authority must never wait for a delivery
                    # lock held by a completer that is itself waiting for that authority.
                    assert (
                        await probe.scalar(
                            text(
                                "SELECT id FROM daily_summary_deliveries "
                                "WHERE id=:id FOR UPDATE NOWAIT"
                            ),
                            {"id": delivery},
                        )
                        == delivery
                    )
                    await probe.rollback()
            finally:
                await transaction.rollback()
                await pending
    finally:
        await engine.dispose()
