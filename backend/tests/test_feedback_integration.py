"""Standalone advisory feedback never approves a report or trusts client provenance."""

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.feedback.adapters.transaction import FeedbackTransactions
from app.modules.feedback.application.feedback_service import FeedbackService
from app.modules.feedback.domain.feedback import FeedbackCommand
from app.modules.reporting.domain.reports import ReportError
from tests.test_report_api_integration import ReportHarness, pytestmark, report_harness
from tests.test_report_review_integration import ready

__all__ = ["pytestmark", "report_harness"]


@pytest.mark.asyncio
async def test_feedback_is_advisory_exact_version_and_replays(report_harness: ReportHarness):
    h = report_harness
    r = await ready(h)
    service = FeedbackService(
        FeedbackTransactions(async_sessionmaker(h.engine, expire_on_commit=False), "UTC")
    )
    key = str(uuid4())
    cmd = FeedbackCommand(
        report_id=r.report.id,
        report_version_id=r.selected_version.id,
        decision="REJECT",
        reason="More context would help",
    )
    first = await service.record(actor=h.actor, command=cmd, idempotency_key=key)
    replay = await service.record(actor=h.actor, command=cmd, idempotency_key=key)
    assert first.feedback.kind == "ADVISORY"
    assert first.feedback.generation_id == r.generation_id
    assert first.feedback.id == replay.feedback.id
    assert replay.replayed
    assert (await h.service.get(actor=h.actor, report_id=r.report.id)).publications == ()
    assert (
        await h.sql(
            "SELECT count(*) FROM report_review_decisions WHERE report_id=:id", {"id": r.report.id}
        )
    ).scalar_one() == 0
    for actor in (h.employee, h.foreign):
        with pytest.raises(ReportError):
            await service.record(actor=actor, command=cmd, idempotency_key=str(uuid4()))
    with pytest.raises(ReportError, match="IDEMPOTENCY_KEY_REUSED"):
        await service.record(
            actor=h.actor,
            command=cmd.model_copy(update={"decision": "ACCEPT"}),
            idempotency_key=key,
        )


@pytest.mark.asyncio
async def test_feedback_api_spoofing_denial_and_rls(report_harness: ReportHarness):
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    h = report_harness
    r = await ready(h)
    body = {
        "report_id": str(r.report.id),
        "report_version_id": str(r.selected_version.id),
        "decision": "ACCEPT",
        "reason": "Helpful",
    }
    headers = {"Idempotency-Key": str(uuid4())}
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        for extra in (
            {"kind": "TERMINAL_QUALITY"},
            {"approved": True},
            {"model_ref": "forged"},
            {"generation_id": str(uuid4())},
        ):
            assert (
                await client.post("/api/v1/feedback", json={**body, **extra}, headers=headers)
            ).status_code == 422
        assert (await client.post("/api/v1/feedback", json=body)).status_code == 422
        first = await client.post("/api/v1/feedback", json=body, headers=headers)
        assert first.status_code == 201, first.text
        replay = await client.post("/api/v1/feedback", json=body, headers=headers)
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert replay.json()["feedback"]["id"] == first.json()["feedback"]["id"]
    for actor in (h.employee, h.foreign):
        async with AsyncClient(
            transport=ASGITransport(app=h.app(actor)), base_url="http://test"
        ) as client:
            assert (
                await client.post("/api/v1/feedback", json=body, headers=headers)
            ).status_code in (403, 404)
        async with h.engine.connect() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true),"
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            for table in ("feedback", "report_version_verifications"):
                assert (
                    await session.execute(text(f"SELECT count(*) FROM {table}"))
                ).scalar_one() == 0
    feedback_id = first.json()["feedback"]["id"]
    with pytest.raises(DBAPIError) as foreign:
        await h.sql(
            "INSERT INTO feedback SELECT :id,organization_id,report_id,report_version_id,"
            "original_version_id,generation_id,:member,decision_id,snapshot_hash,kind,"
            "decision,reason,provenance,created_at FROM feedback WHERE id=:original",
            {"id": uuid4(), "member": h.foreign.membership_id, "original": feedback_id},
        )
    assert getattr(foreign.value.orig, "sqlstate", None) == "23503"
    with pytest.raises(DBAPIError):
        await h.sql("UPDATE feedback SET decision='REJECT' WHERE id=:id", {"id": feedback_id})


@pytest.mark.asyncio
async def test_malformed_feedback_still_has_rejection_audit(report_harness: ReportHarness):
    from httpx import ASGITransport, AsyncClient

    h = report_harness
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/feedback",
            json={"decision": "ACCEPT", "reason": "Helpful"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 422
    assert (
        await h.sql(
            "SELECT count(*) FROM audit_events WHERE organization_id=:org "
            "AND action='feedback.recorded' AND outcome='REJECTED'",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 1
