"""SQL/RLS, durable job replay, review and notification boundaries."""

import os
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.identity.api.dependencies import get_authenticated_actor
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.risk.adapters.repository import RiskTransactions
from app.modules.risk.application.risk_service import RiskService
from app.modules.risk.domain.assessments import RiskInputs, RiskJudgment
from tests.test_daily_update_api_integration import daily_app, seed_task
from tests.test_evidence_api_integration import Harness, harness

__all__ = ["harness"]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


class Model:
    calls = 0

    async def assess(self, inputs: RiskInputs):
        self.calls += 1
        return RiskJudgment.model_validate(
            {
                "score": "83",
                "rationale": "Review deadline and blocker.",
                "observations": [{"text": "Deadline", "source_ids": [inputs.facts[0].id]}],
                "recommendations": ["Discuss the deadline."],
            }
        ), "mock-test"


async def setup(h: Harness):
    await h.sql("UPDATE memberships SET role='MANAGER' WHERE id=:id", {"id": h.peer.membership_id})
    manager = replace(h.peer, role=MembershipRole.MANAGER)
    sessions = async_sessionmaker(
        bind=h.connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    model = Model()
    service = RiskService(RiskTransactions(sessions), model)
    app = daily_app(h)
    app.state.risk_service = service
    app.dependency_overrides[get_authenticated_actor] = lambda: manager
    return app, service, model, manager


async def severe_blocker(h: Harness, manager: AuthenticatedActor, task: UUID) -> None:
    from app.modules.progress.adapters.blocker_repository import SqlAlchemyBlockerTransactions
    from app.modules.progress.application.blocker_service import BlockerService
    from app.modules.progress.domain.blockers import BlockerCommand

    sessions = async_sessionmaker(
        bind=h.connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    await BlockerService(SqlAlchemyBlockerTransactions(sessions)).apply(
        manager,
        BlockerCommand(
            task_id=task,
            expected_task_version=1,
            action="CREATE",
            severity="HIGH",
            text="Awaiting materials",
        ),
        str(uuid4()),
    )


@pytest.mark.asyncio
async def test_ai_risk_score_preserved_and_notification_permission(harness: Harness):
    task = await seed_task(harness)
    app, service, model, manager = await setup(harness)
    from datetime import UTC, datetime, timedelta

    await harness.sql(
        "UPDATE tasks SET due_date=:due WHERE id=:id",
        {"id": UUID(task), "due": datetime.now(UTC).date() - timedelta(days=1)},
    )
    await severe_blocker(harness, manager, UUID(task))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        key = str(uuid4())
        response = await c.post(
            "/api/v1/risks/refresh", json={"task_id": str(task)}, headers={"Idempotency-Key": key}
        )
        assert response.status_code == 202, response.text
        assert response.json()["state"] == "PENDING"
        assert await service.run_once(manager)
        result = (await c.get(f"/api/v1/risks?task_id={task}")).json()
        assert result["judgment"]["score"] == "83" and result["band"] == "HIGH"
        assert any(f["kind"] == "BLOCKER" for f in result["input_snapshot"]["facts"])
        assert result["input_snapshot"]["facts"][0]["values"]["overdue"]
        assert (
            await c.post(
                "/api/v1/risks/refresh",
                json={"task_id": str(task)},
                headers={"Idempotency-Key": key},
            )
        ).status_code == 202
        assert not await service.run_once(manager)
        assert model.calls == 1
        notices = (await c.get("/api/v1/notifications")).json()
        assert {n["kind"] for n in notices} == {"HIGH_RISK", "SEVERE_BLOCKER"}
        review = {"disposition": "ACCEPTED_EXPLANATION", "reason": "Supplier confirmed delivery."}
        headers = {"Idempotency-Key": str(uuid4())}
        for _ in range(2):
            assert (
                await c.post(f"/api/v1/risks/{result['id']}/reviews", json=review, headers=headers)
            ).status_code == 201
        assert len((await c.get(f"/api/v1/risks/{result['id']}/reviews")).json()) == 1
        assert (await c.get(f"/api/v1/risks?task_id={task}")).json()["judgment"]["score"] == "83"
        app.dependency_overrides[get_authenticated_actor] = lambda: harness.actor
        denied = await c.get(f"/api/v1/risks?task_id={task}")
        assert denied.status_code == 403, denied.text
        app.dependency_overrides[get_authenticated_actor] = lambda: harness.foreign
        assert (await c.get(f"/api/v1/risks?task_id={task}")).status_code == 404
        assert (await c.get("/api/v1/notifications")).json() == []


@pytest.mark.asyncio
async def test_invalid_provider_result_manual_review_and_revocation(harness: Harness):
    task = await seed_task(harness)
    app, service, _model, manager = await setup(harness)

    class Failed(Model):
        async def assess(self, inputs: RiskInputs):
            raise TimeoutError()

    service.model = Failed()
    await service.refresh(manager, UUID(task), uuid4())
    assert await service.run_once(manager)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        result = (await c.get(f"/api/v1/risks?task_id={task}")).json()
        assert (
            result["state"] == "UNAVAILABLE"
            and result["judgment"] is None
            and result["band"] is None
        )
        assert (
            await c.post(
                f"/api/v1/risks/{result['id']}/reviews",
                json={"disposition": "NEEDS_FOLLOWUP", "reason": "Review manually."},
                headers={"Idempotency-Key": str(uuid4())},
            )
        ).status_code == 201


@pytest.mark.asyncio
async def test_duplicate_cause_stale_context_reviews_read_and_direct_rls(harness: Harness):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.modules.progress.domain.blockers import BlockerError
    from app.modules.risk.application.notification_service import NotificationService

    task = UUID(await seed_task(harness))
    app, service, model, manager = await setup(harness)
    cause = uuid4()
    queued = await service.refresh(manager, task, cause)
    assert await service.run_once(manager)
    replay = await service.refresh(manager, task, cause)
    assert replay.id == queued.id and replay.judgment and replay.judgment.score == 83
    assert not await service.run_once(manager) and model.calls == 1
    notices = await NotificationService(service.transactions).list(manager)
    assert len(notices) == 1
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        headers = {"Idempotency-Key": str(uuid4())}
        for _ in range(2):
            response = await c.post(f"/api/v1/notifications/{notices[0].id}/read", headers=headers)
            assert response.status_code == 200 and response.json()["read"]
        assert (
            await c.post(
                f"/api/v1/risks/{queued.id}/reviews",
                json={"disposition": "RESOLVED", "reason": "   "},
                headers={"Idempotency-Key": str(uuid4())},
            )
        ).status_code == 422
        app.dependency_overrides[get_authenticated_actor] = lambda: harness.foreign
        assert (
            await c.post(
                f"/api/v1/notifications/{notices[0].id}/read",
                headers={"Idempotency-Key": str(uuid4())},
            )
        ).status_code == 404
        assert (
            await c.post(
                f"/api/v1/risks/{queued.id}/reviews",
                json={"disposition": "RESOLVED", "reason": "Foreign tenant request"},
                headers={"Idempotency-Key": str(uuid4())},
            )
        ).status_code == 404
        assert (
            await c.post(
                "/api/v1/risks/refresh",
                json={"task_id": str(task)},
                headers={"Idempotency-Key": str(uuid4())},
            )
        ).status_code == 404
    for actor in (harness.actor, harness.foreign):
        async with harness.connection.begin_nested():
            await harness.connection.execute(text("SET LOCAL ROLE app_runtime"))
            await harness.connection.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true), "
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            assert (
                await harness.connection.execute(
                    text("SELECT count(*) FROM risk_assessments WHERE id=:id"), {"id": queued.id}
                )
            ).scalar_one() == 0
    await harness.sql("SELECT 1")
    with pytest.raises(DBAPIError):
        async with harness.connection.begin_nested():
            await harness.connection.execute(
                text("UPDATE risk_assessments SET payload='{}' WHERE id=:id"), {"id": queued.id}
            )
    await harness.sql("UPDATE tasks SET version=version+1 WHERE id=:id", {"id": task})
    current = await service.current(manager, task)
    assert (
        current and current.state == "STALE" and current.judgment is None and current.band is None
    )
    await harness.sql(
        "UPDATE memberships SET is_active=false WHERE id=:id", {"id": manager.membership_id}
    )
    with pytest.raises(BlockerError):
        await NotificationService(service.transactions).deliver(manager, queued.id)
    assert len(notices) == 1


@pytest.mark.asyncio
async def test_context_changes_during_model_call_and_exhausted_lease(harness: Harness):
    from datetime import UTC, datetime, timedelta

    from app.modules.risk.application.ports import RiskLease

    task = UUID(await seed_task(harness))
    _app, service, _model, manager = await setup(harness)

    class Changed(Model):
        async def assess(self, inputs: RiskInputs):
            await harness.sql("UPDATE tasks SET version=version+1 WHERE id=:id", {"id": task})
            return await super().assess(inputs)

    service.model = Changed()
    await service.refresh(manager, task, uuid4())
    assert await service.run_once(manager)
    result = await service.current(manager, task)
    assert result and result.state == "STALE" and result.judgment is None
    service.model = Model()
    queued = await service.refresh(manager, task, uuid4())
    async with service.transactions(manager) as repo:
        await repo.authenticate()
        lease = await repo.claim()
        assert isinstance(lease, RiskLease)
    await harness.sql(
        "UPDATE risk_refresh_jobs SET attempts=3,lease_until=:expired WHERE id=:id",
        {"id": queued.id, "expired": datetime.now(UTC) - timedelta(seconds=1)},
    )
    assert not await service.run_once(manager)
    result = await service.current(manager, task)
    assert result and result.state == "UNAVAILABLE" and result.limitation == "REFRESH_EXPIRED"


@pytest.mark.asyncio
async def test_outbox_retry_neighborhood_and_revoked_recipient(harness: Harness):
    from app.modules.identity.adapters.auth_repository import SqlAlchemyAuthTransactionFactory
    from app.modules.identity.adapters.current_actor import CurrentActorResolver
    from app.modules.identity.application.current_actor_service import CurrentActorService
    from app.modules.planning_runs.domain.models import OutboxEvent
    from app.modules.risk.adapters.outbox_consumer import RiskOutboxPublisher, RiskWorker

    task = UUID(await seed_task(harness))
    _app, service, model, manager = await setup(harness)
    sessions = async_sessionmaker(
        bind=harness.connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    worker = RiskWorker(
        sessions,
        service,
        CurrentActorResolver(CurrentActorService(SqlAlchemyAuthTransactionFactory(sessions))),
    )
    publisher = RiskOutboxPublisher(worker)
    event = OutboxEvent(
        id=uuid4(),
        event_id=uuid4(),
        organization_id=manager.organization_id,
        event_type="daily_update.confirmed",
        aggregate_type="daily_update",
        aggregate_id=uuid4(),
        payload={"task_ids": [str(task)]},
    )
    for _ in range(2):
        await publisher.publish(event)
    assert await service.run_once(manager)
    assert not await service.run_once(manager) and model.calls == 1
    result = await service.current(manager, task)
    assert result
    event = replace(event, event_type="risk.assessed.v1", payload={"risk_id": str(result.id)})
    for _ in range(2):
        await publisher.publish(event)
    await harness.sql(
        "UPDATE memberships SET is_active=false WHERE id=:id", {"id": manager.membership_id}
    )
    await publisher.publish(event)
    assert (
        await harness.sql(
            "SELECT count(*) FROM risk_notifications WHERE risk_id=:id", {"id": result.id}
        )
    ).scalar_one() == 1


@pytest.mark.asyncio
async def test_remaining_work_cross_project_uses_confirmed_hours_not_estimates(harness: Harness):
    from datetime import UTC, datetime
    from decimal import Decimal

    from app.modules.people_capacity.application.workload_service import remaining_work
    from app.modules.risk.adapters.repository import RiskRepository
    from tests.test_daily_update_api_integration import body, confirm, draft

    first, second, unknown = [await seed_task(harness) for _ in range(3)]
    app, service, _model, manager = await setup(harness)
    app.dependency_overrides[get_authenticated_actor] = lambda: harness.actor
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for task, hours in ((first, "8"), (second, "7")):
            d = await draft(c, [body(task, reported_percent="30", remaining_hours=hours)])
            response = await confirm(c, d)
            assert response.status_code == 201, response.text
    async with service.transactions(manager) as repo:
        assert isinstance(repo, RiskRepository)
        await repo.authenticate()
        raw = await repo.load_remaining_work(
            actor=manager,
            membership_id=harness.actor.membership_id,
            observed_on=datetime.now(UTC).date(),
        )
        result = remaining_work(raw)
        assert result.known_hours == Decimal(15) and result.unknown_count == 1
        assert result.available_hours is None and result.overload is None
        assert unknown and raw.weekly_capacity is None


@pytest.mark.asyncio
async def test_confirmed_ignored_warnings_remain_reviewable_after_new_report(harness: Harness):
    from app.modules.risk.adapters.repository import RiskRepository
    from app.modules.risk.application.notification_service import NotificationService
    from tests.test_daily_update_api_integration import body, confirm, draft
    from tests.test_evidence_api_integration import upload
    from tests.test_evidence_assessment_integration import assess, assessed_app

    task = await seed_task(harness)
    _app, service, _model, manager = await setup(harness)
    app = assessed_app(harness)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        proof = (await upload(c)).json()
        d = await draft(
            c, [body(task, evidence_refs=[{"evidence_id": proof["evidence_id"], "version": 1}])]
        )
        assessment = await assess(c, d)
        response = await c.post(
            f"/api/v1/daily-updates/{d['id']}/confirm",
            json={
                "draft_id": d["id"],
                "expected_draft_version": d["version"],
                "assessment_id": assessment["id"],
                "warning_acknowledgments": [w["id"] for w in assessment["warnings"]],
            },
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 201, response.text
        d = await draft(c, [body(task, expected_progress_version=1, remaining_hours="2")])
        response = await confirm(c, d)
        assert response.status_code == 201, response.text
    async with service.transactions(manager) as repo:
        assert isinstance(repo, RiskRepository)
        inputs = await repo.inputs(UUID(task))
        assert any(
            f.kind == "WARNING" and f.values["assessment_id"] == assessment["id"]
            for f in inputs.facts
        )
    await service.refresh(manager, UUID(task), uuid4())
    assert await service.run_once(manager)
    assert {n.kind for n in await NotificationService(service.transactions).list(manager)} == {
        "HIGH_RISK",
        "EVIDENCE_REVIEW",
    }


@pytest.mark.asyncio
async def test_worker_reconciles_manual_changes_and_next_reporting_day(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
):
    from datetime import UTC, datetime, timedelta, tzinfo

    from app.modules.identity.adapters.auth_repository import SqlAlchemyAuthTransactionFactory
    from app.modules.identity.adapters.current_actor import CurrentActorResolver
    from app.modules.identity.application.current_actor_service import CurrentActorService
    from app.modules.risk.adapters import repository as risk_repository
    from app.modules.risk.adapters.outbox_consumer import RiskWorker

    task = UUID(await seed_task(harness))
    _app, service, model, manager = await setup(harness)
    sessions = async_sessionmaker(
        bind=harness.connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    worker = RiskWorker(
        sessions,
        service,
        CurrentActorResolver(CurrentActorService(SqlAlchemyAuthTransactionFactory(sessions))),
    )

    async def run():
        return await worker.run_once(worker_id="risk-test", organization_id=manager.organization_id)

    assert await run() and model.calls == 1
    assert not await run()
    assert not await run() and model.calls == 1
    await harness.sql("UPDATE tasks SET version=version+1 WHERE id=:id", {"id": task})
    assert not await run()  # Finish this cursor page; next poll starts the bounded scan again.
    assert await run() and model.calls == 2
    future = datetime.now(UTC) + timedelta(days=1)

    class Tomorrow(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None):
            return future.astimezone(tz) if tz else future.replace(tzinfo=None)

    monkeypatch.setattr(risk_repository, "datetime", Tomorrow)
    assert not await run()
    assert await run() and model.calls == 3
    assert (
        await harness.sql("SELECT count(*) FROM risk_notifications WHERE task_id=:id", {"id": task})
    ).scalar_one() == 2


@pytest.mark.asyncio
async def test_oversized_task_terminalizes_job_and_does_not_poison_later_jobs(harness: Harness):
    from datetime import UTC, datetime, timedelta

    from app.modules.risk.adapters.repository import RiskRepository

    first, second = UUID(await seed_task(harness)), UUID(await seed_task(harness))
    _app, service, model, manager = await setup(harness)
    queued = await service.refresh(manager, first, uuid4())
    await service.refresh(manager, second, uuid4())
    for _ in range(100):
        await harness.sql(
            "INSERT INTO blockers(id,organization_id,task_id,created_by_membership_id,"
            "version,severity,status,archived,payload,created_at,updated_at) "
            "VALUES (:id,:org,:task,:member,1,'HIGH','OPEN',false,:payload,:now,:now)",
            {
                "id": uuid4(),
                "org": manager.organization_id,
                "task": first,
                "member": manager.membership_id,
                "payload": '{"text":"Awaiting materials"}',
                "now": datetime.now(UTC),
            },
        )
    # Even expiration must not depend on reconstructing an oversized model context.
    await harness.sql(
        "UPDATE risk_refresh_jobs SET deadline=:expired WHERE id=:id",
        {"id": queued.id, "expired": datetime.now(UTC) - timedelta(seconds=1)},
    )
    assert not await service.run_once(manager)
    assert await service.run_once(manager) and model.calls == 1
    async with service.transactions(manager) as repo:
        assert isinstance(repo, RiskRepository)
        result = await repo.get(queued.id)
        assert result.state == "UNAVAILABLE" and result.judgment is None
        assert result.limitation == "RISK_CONTEXT_LIMIT"
    assert (
        await harness.sql("SELECT state FROM risk_refresh_jobs WHERE id=:id", {"id": queued.id})
    ).scalar_one() == "FAILED"


@pytest.mark.asyncio
async def test_confirmed_severe_blocker_alert_without_model_score(harness: Harness):
    from app.modules.risk.application.notification_service import NotificationService

    task = UUID(await seed_task(harness))
    _app, service, _model, manager = await setup(harness)
    await severe_blocker(harness, manager, task)

    class Failed(Model):
        async def assess(self, inputs: RiskInputs):
            raise TimeoutError()

    service.model = Failed()
    for _ in range(2):
        await service.refresh(manager, task, uuid4())
        assert await service.run_once(manager)
    result = await service.current(manager, task)
    assert (
        result and result.state == "UNAVAILABLE" and result.judgment is None and result.band is None
    )
    notices = await NotificationService(service.transactions).list(manager)
    assert len(notices) == 1 and notices[0].kind == "SEVERE_BLOCKER"
