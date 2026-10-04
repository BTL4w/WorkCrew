"""Manual exact-version publications are atomic, replay-safe and tenant-authorized."""

from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from tests.test_report_api_integration import ReportHarness, pytestmark, report_harness

__all__ = ["pytestmark", "report_harness"]


def publish_body(report: dict[str, Any]) -> dict[str, str]:
    return {
        "mode": "METRICS_ONLY",
        "report_version_id": report["selected_version"]["id"],
        "snapshot_hash": report["snapshot"]["snapshot_hash"],
    }


@pytest.mark.asyncio
async def test_metrics_publish_direct_and_idempotent(report_harness: ReportHarness):
    h = report_harness
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        made = await client.post(
            "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
        )
        report = made.json()
        rid = report["report"]["id"]
        headers = {"Idempotency-Key": str(uuid4()), "If-Match": '"1"'}
        first = await client.post(
            f"/api/v1/reports/{rid}/publish", json=publish_body(report), headers=headers
        )
        assert first.status_code == 201, first.text
        result = first.json()
        assert result["report"]["version"] == 2
        assert result["report"]["selected_version_id"] == report["report"]["selected_version_id"]
        assert len(result["publications"]) == 1
        publication = result["publications"][0]
        assert publication["snapshot_hash"] == report["snapshot"]["snapshot_hash"]
        assert publication["report_version_id"] == report["selected_version"]["id"]
        assert publication["publisher_membership_id"] == str(h.actor.membership_id)
        assert result["report"]["current_publication_id"] == publication["id"]
        replay = await client.post(
            f"/api/v1/reports/{rid}/publish", json=publish_body(report), headers=headers
        )
        assert replay.status_code == 201, replay.text
        assert replay.json()["publications"] == result["publications"]
        assert replay.json()["replayed"]
        changed = await client.post(
            f"/api/v1/reports/{rid}/publish",
            json={**publish_body(report), "snapshot_hash": "a" * 64},
            headers=headers,
        )
        assert changed.status_code == 409
        assert changed.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
        await h.sql(
            "UPDATE tasks SET title='New survey',version=version+1 WHERE id=:id", {"id": h.task_id}
        )
        history = await client.get(f"/api/v1/reports/{rid}")
        assert history.json()["snapshot"] == report["snapshot"]
        assert history.json()["publications"] == result["publications"]
    for table in ("report_publications", "report_review_decisions"):
        assert (
            await h.sql(
                f"SELECT count(*) FROM {table} WHERE organization_id=:org",
                {"org": h.actor.organization_id},
            )
        ).scalar_one() == 1
    assert (
        await h.sql(
            "SELECT count(*) FROM outbox_events WHERE organization_id=:org "
            "AND event_type='report.published.v1'",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 1
    assert (
        await h.sql(
            "SELECT count(*) FROM audit_events WHERE organization_id=:org "
            "AND action='report.published' AND outcome='SUCCEEDED'",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 1


@pytest.mark.asyncio
async def test_two_tabs_cannot_publish_stale_version(report_harness: ReportHarness):
    h = report_harness
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        made = (
            await client.post(
                "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
            )
        ).json()
        rid = made["report"]["id"]
        await h.sql("UPDATE reports SET version=2 WHERE id=:id", {"id": rid})
        stale = await client.post(
            f"/api/v1/reports/{rid}/publish",
            json=publish_body(made),
            headers={"Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
        )
        assert stale.status_code == 412, stale.text
        assert stale.json()["error"]["code"] == "STALE_REPORT_VERSION"
        missing = await client.post(
            f"/api/v1/reports/{rid}/publish",
            json=publish_body(made),
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert missing.status_code == 428
        invalid = await client.post(
            f"/api/v1/reports/{rid}/publish",
            json=publish_body(made),
            headers={"Idempotency-Key": str(uuid4()), "If-Match": "garbage"},
        )
        assert invalid.status_code == 400
    assert (
        await h.sql(
            "SELECT count(*) FROM report_publications WHERE organization_id=:org",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 0
    assert (
        await h.sql(
            "SELECT count(*) FROM report_review_decisions WHERE organization_id=:org",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 0
    assert (
        await h.sql(
            "SELECT count(*) FROM audit_events WHERE organization_id=:org "
            "AND action='report.published' AND outcome='REJECTED'",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 3


@pytest.mark.asyncio
async def test_snapshot_hash_and_tenant_are_checked(report_harness: ReportHarness):
    h = report_harness
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        made = (
            await client.post(
                "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
            )
        ).json()
        rid = made["report"]["id"]
        second = (
            await client.post(
                "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
            )
        ).json()
        for changes in (
            {"snapshot_hash": "a" * 64},
            {"report_version_id": second["selected_version"]["id"]},
        ):
            denied = await client.post(
                f"/api/v1/reports/{rid}/publish",
                json={**publish_body(made), **changes},
                headers={"Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
            )
            assert denied.status_code == 409, denied.text
    for actor, status in ((h.employee, 403), (h.foreign, 404)):
        async with AsyncClient(
            transport=ASGITransport(app=h.app(actor)), base_url="http://test"
        ) as client:
            denied = await client.post(
                f"/api/v1/reports/{rid}/publish",
                json=publish_body(made),
                headers={"Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
            )
            assert denied.status_code == status, denied.text
    await h.sql(
        "UPDATE memberships SET role='EMPLOYEE' WHERE id=:id", {"id": h.actor.membership_id}
    )
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        denied = await client.post(
            f"/api/v1/reports/{rid}/publish",
            json=publish_body(made),
            headers={"Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
        )
        assert denied.status_code == 403, denied.text


@pytest.mark.asyncio
async def test_pending_ai_does_not_change_existing_publication(report_harness: ReportHarness):
    h = report_harness
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        made = (
            await client.post(
                "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
            )
        ).json()
        rid = made["report"]["id"]
        published = await client.post(
            f"/api/v1/reports/{rid}/publish",
            json=publish_body(made),
            headers={"Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
        )
        assert published.status_code == 201, published.text
        assert published.json()["generation_state"] == "QUEUED"
        # A later draft pointer change is independent of the immutable publication pointer.
        await h.sql("UPDATE reports SET version=3 WHERE id=:id", {"id": rid})
        current = await client.get(f"/api/v1/reports/{rid}")
        assert current.json()["publications"] == published.json()["publications"]
        assert (
            current.json()["report"]["current_publication_id"]
            == published.json()["report"]["current_publication_id"]
        )


@pytest.mark.asyncio
async def test_concurrent_publications_replay_and_stale_tabs(report_harness: ReportHarness):
    import asyncio

    h = report_harness
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        for same_key in (True, False):
            made = (
                await client.post(
                    "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
                )
            ).json()
            rid = made["report"]["id"]
            key = str(uuid4())
            results = await asyncio.gather(
                *[
                    client.post(
                        f"/api/v1/reports/{rid}/publish",
                        json=publish_body(made),
                        headers={
                            "Idempotency-Key": key if same_key else str(uuid4()),
                            "If-Match": '"1"',
                        },
                    )
                    for _ in range(2)
                ]
            )
            assert sorted(response.status_code for response in results) == (
                [201, 201] if same_key else [201, 412]
            ), [response.text for response in results]
            current = (await client.get(f"/api/v1/reports/{rid}")).json()
            assert len(current["publications"]) == 1
            assert current["report"]["version"] == 2
            if same_key:
                assert results[0].json()["publications"] == results[1].json()["publications"]
                # A later draft version does not change the saved response to the old intent.
                await h.sql("UPDATE reports SET version=3 WHERE id=:id", {"id": rid})
                replay = await client.post(
                    f"/api/v1/reports/{rid}/publish",
                    json=publish_body(made),
                    headers={"Idempotency-Key": key, "If-Match": '"1"'},
                )
                assert replay.status_code == 201
                assert replay.json()["report"]["version"] == 2
                assert replay.headers["Idempotency-Replayed"] == "true"
                await h.sql(
                    "UPDATE memberships SET role='EMPLOYEE' WHERE id=:id",
                    {"id": h.actor.membership_id},
                )
                denied = await client.post(
                    f"/api/v1/reports/{rid}/publish",
                    json=publish_body(made),
                    headers={"Idempotency-Key": key, "If-Match": '"1"'},
                )
                assert denied.status_code == 403
                await h.sql(
                    "UPDATE memberships SET role='MANAGER' WHERE id=:id",
                    {"id": h.actor.membership_id},
                )


@pytest.mark.asyncio
async def test_failed_publication_rolls_back_all_business_effects(
    report_harness: ReportHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.modules.reporting.adapters.repository import SQLReportRepository
    from app.modules.reporting.domain.reports import ReportError

    h = report_harness
    publish = SQLReportRepository.publish

    async def fail_after_flush(repo: SQLReportRepository, *args: Any, **kwargs: Any):
        await publish(repo, *args, **kwargs)
        raise ReportError("TEST_ROLLBACK")

    monkeypatch.setattr(SQLReportRepository, "publish", fail_after_flush)
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        made = (
            await client.post(
                "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
            )
        ).json()
        rid = made["report"]["id"]
        failed = await client.post(
            f"/api/v1/reports/{rid}/publish",
            json=publish_body(made),
            headers={"Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
        )
        assert failed.status_code == 409
        current = (await client.get(f"/api/v1/reports/{rid}")).json()
        assert current["report"]["version"] == 1
        assert current["report"]["current_publication_id"] is None
    for table in ("report_publications", "report_review_decisions"):
        assert (
            await h.sql(
                f"SELECT count(*) FROM {table} WHERE organization_id=:org",
                {"org": h.actor.organization_id},
            )
        ).scalar_one() == 0
    assert (
        await h.sql(
            "SELECT count(*) FROM outbox_events WHERE organization_id=:org "
            "AND event_type='report.published.v1'",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 0
    assert (
        await h.sql(
            "SELECT count(*) FROM audit_events WHERE organization_id=:org "
            "AND action='report.published' AND outcome='REJECTED'",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 1


@pytest.mark.asyncio
async def test_publication_database_ownership_and_immutability(report_harness: ReportHarness):
    from sqlalchemy import insert, select, text
    from sqlalchemy.exc import DBAPIError

    from app.modules.reporting.adapters.database_models import (
        ReportPublicationModel,
        ReportReviewDecisionModel,
    )

    h = report_harness
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        made = (
            await client.post(
                "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
            )
        ).json()
        rid = made["report"]["id"]
        published = await client.post(
            f"/api/v1/reports/{rid}/publish",
            json=publish_body(made),
            headers={"Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
        )
        assert published.status_code == 201
        publication = published.json()["publications"][0]
        second = (
            await client.post(
                "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
            )
        ).json()
    for model in (ReportPublicationModel, ReportReviewDecisionModel):
        table = model.__tablename__
        async with h.engine.begin() as connection:
            original = dict(
                (
                    await connection.execute(
                        select(model.__table__).where(
                            model.organization_id == h.actor.organization_id
                        )
                    )
                )
                .mappings()
                .one()
            )
        for actor in (h.foreign, h.employee):
            with pytest.raises(DBAPIError) as denial:
                async with h.engine.begin() as connection:
                    await connection.execute(text("SET LOCAL ROLE app_runtime"))
                    await connection.execute(
                        text(
                            "SELECT set_config('app.organization_id',:org,true), "
                            "set_config('app.membership_id',:member,true)"
                        ),
                        {"org": str(actor.organization_id), "member": str(actor.membership_id)},
                    )
                    await connection.execute(insert(model).values(**{**original, "id": uuid4()}))
            assert getattr(denial.value.orig, "sqlstate", None) == "42501"
        with pytest.raises(DBAPIError):
            await h.sql(
                f"UPDATE {table} SET snapshot_hash=:hash WHERE organization_id=:org",
                {"hash": "b" * 64, "org": h.actor.organization_id},
            )
        with pytest.raises(DBAPIError):
            await h.sql(
                f"DELETE FROM {table} WHERE organization_id=:org", {"org": h.actor.organization_id}
            )
        for actor in (h.foreign, h.employee):
            async with h.engine.begin() as connection:
                await connection.execute(text("SET LOCAL ROLE app_runtime"))
                await connection.execute(
                    text(
                        "SELECT set_config('app.organization_id',:org,true), "
                        "set_config('app.membership_id',:member,true)"
                    ),
                    {"org": str(actor.organization_id), "member": str(actor.membership_id)},
                )
                assert (
                    await connection.execute(text(f"SELECT count(*) FROM {table}"))
                ).scalar_one() == 0
    with pytest.raises(DBAPIError):
        await h.sql(
            "UPDATE reports SET current_publication_id=:publication WHERE id=:id",
            {"publication": publication["id"], "id": second["report"]["id"]},
        )
    for column, value in (
        ("organization_id", h.foreign.organization_id),
        ("publisher_membership_id", h.employee.membership_id),
        ("report_version_id", second["selected_version"]["id"]),
        ("snapshot_hash", "b" * 64),
    ):
        new_decision = uuid4()
        await h.sql(
            "INSERT INTO report_review_decisions (id,organization_id,report_id,report_version_id,"
            "snapshot_hash,actor_membership_id,expected_report_version,kind,decided_at) "
            "SELECT :new_id,organization_id,report_id,report_version_id,snapshot_hash,"
            "actor_membership_id,expected_report_version,kind,decided_at "
            "FROM report_review_decisions WHERE id=:id",
            {"new_id": new_decision, "id": publication["decision_id"]},
        )
        with pytest.raises(DBAPIError) as mismatch:
            await h.sql(
                "INSERT INTO report_publications (id,organization_id,report_id,report_version_id,"
                "snapshot_hash,publisher_membership_id,decision_id,published_at) "
                f"SELECT :new_id,{':value' if column == 'organization_id' else 'organization_id'},"
                f"report_id,{':value' if column == 'report_version_id' else 'report_version_id'},"
                f"{':value' if column == 'snapshot_hash' else 'snapshot_hash'},"
                f"{':value' if column == 'publisher_membership_id' else 'publisher_membership_id'},"
                ":decision,published_at FROM report_publications WHERE id=:id",
                {
                    "new_id": uuid4(),
                    "value": value,
                    "id": publication["id"],
                    "decision": new_decision,
                },
            )

        assert getattr(mismatch.value.orig, "sqlstate", None) == "23503"
    with pytest.raises(DBAPIError) as duplicate:
        await h.sql(
            "INSERT INTO report_publications (id,organization_id,report_id,report_version_id,"
            "snapshot_hash,publisher_membership_id,decision_id,published_at) "
            "SELECT :new_id,organization_id,report_id,report_version_id,snapshot_hash,"
            "publisher_membership_id,decision_id,published_at "
            "FROM report_publications WHERE id=:id",
            {"new_id": uuid4(), "id": publication["id"]},
        )
    assert getattr(duplicate.value.orig, "sqlstate", None) == "23505"


@pytest.mark.asyncio
async def test_invalid_publish_transport_is_audited(report_harness: ReportHarness):
    h = report_harness
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        made = (
            await client.post(
                "/api/v1/reports", json=h.body(), headers={"Idempotency-Key": str(uuid4())}
            )
        ).json()
        rid = made["report"]["id"]
        for body, headers in (
            (publish_body(made), {"If-Match": '"1"'}),
            (
                {**publish_body(made), "mode": "AI_APPROVED"},
                {"Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
            ),
        ):
            rejected = await client.post(
                f"/api/v1/reports/{rid}/publish", json=body, headers=headers
            )
            assert rejected.status_code == 422
    assert (
        await h.sql(
            "SELECT count(*) FROM audit_events WHERE organization_id=:org "
            "AND action='report.published' AND outcome='REJECTED'",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 2
