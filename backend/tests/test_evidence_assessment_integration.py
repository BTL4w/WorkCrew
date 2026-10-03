"""Real database contracts: warnings cannot be bypassed through either submit route."""

import os
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.identity.api.dependencies import get_authenticated_actor
from app.modules.progress.application.assessment_service import (
    ComparisonBudget,
    EvidenceComparisonResult,
    OriginalSource,
)
from app.modules.progress.domain.daily_updates import SelectedEvidence
from app.modules.progress.domain.evidence_support import (
    Claim,
    ClaimFinding,
    EvidenceJudgment,
    FindingKind,
)
from tests.test_daily_update_api_integration import body, daily_app, draft, seed_task
from tests.test_evidence_api_integration import Harness, harness, upload

__all__ = ["harness"]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


class Comparison:
    def __init__(self, kind: FindingKind = "UNSUPPORTED", failure: Exception | None = None):
        self.kind: FindingKind = kind
        self.failure = failure
        self.score = Decimal("100") if kind == "SUPPORTED" else Decimal("0")

    async def compare(
        self,
        claims: tuple[Claim, ...],
        original_sources: tuple[OriginalSource, ...],
        budget: ComparisonBudget,
    ) -> EvidenceComparisonResult:

        if self.failure:
            raise self.failure
        return EvidenceComparisonResult(
            judgment=EvidenceJudgment(score=self.score, rationale="Mock assessment for this test."),
            findings=tuple(
                ClaimFinding(claim_id=c.id, finding=self.kind, source_refs=c.evidence_refs)
                for c in claims
            ),
            processed_sources=tuple(
                SelectedEvidence(evidence_id=s.evidence_id, version=s.version)
                for s in original_sources
            ),
        )


def assessed_app(h: Harness, comparison: Comparison | None = None) -> FastAPI:
    from app.modules.progress.adapters.assessment_repository import SqlAlchemyAssessmentTransactions
    from app.modules.progress.application.assessment_service import AssessmentService

    app = daily_app(h)
    sessions = async_sessionmaker(
        bind=h.connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    app.state.daily_update_service.assessment_available = True
    app.state.assessment_service = AssessmentService(
        SqlAlchemyAssessmentTransactions(sessions), comparison or Comparison()
    )
    return app


async def assess(c: AsyncClient, d: dict[str, Any], key: str | None = None) -> dict[str, Any]:
    response = await c.post(
        f"/api/v1/daily-updates/{d['id']}/assess",
        json={"expected_version": d["version"]},
        headers={"Idempotency-Key": key or str(uuid4())},
    )
    assert response.status_code == 202, response.text
    current = await c.get(f"/api/v1/daily-updates/{d['id']}/evidence-assessments")
    assert current.status_code == 200, current.text
    return current.json()


@pytest.mark.asyncio
async def test_warning_ack_exact_snapshot_replay_and_no_bypass(harness: Harness):
    task = await seed_task(harness)
    app = assessed_app(harness)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        proof = (await upload(c)).json()
        d = await draft(
            c, [body(task, evidence_refs=[{"evidence_id": proof["evidence_id"], "version": 1}])]
        )
        key = str(uuid4())
        a = await assess(c, d, key)
        assert (await assess(c, d, key))["id"] == a["id"]
        assert a["state"] == "READY" and a["result"]["score"] == "0"
        command = {"draft_id": d["id"], "expected_draft_version": 1}
        for url in ("/api/v1/daily-updates", f"/api/v1/daily-updates/{d['id']}/confirm"):
            for change in (
                {},
                {"assessment_id": a["id"]},
                {"assessment_id": a["id"], "warning_acknowledgments": [str(uuid4())]},
            ):
                response = await c.post(
                    url, json={**command, **change}, headers={"Idempotency-Key": str(uuid4())}
                )
                assert response.status_code == 409, response.text
        response = await c.post(
            "/api/v1/daily-updates",
            json={**command, "provider_unavailable": True},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 422
        command.update(
            assessment_id=a["id"], warning_acknowledgments=[w["id"] for w in a["warnings"]]
        )
        key = str(uuid4())
        saved = await c.post(
            "/api/v1/daily-updates", json=command, headers={"Idempotency-Key": key}
        )
        assert saved.status_code == 201, saved.text
        assert saved.json()["assessment_state"] == "READY"
        assert (
            await c.post("/api/v1/daily-updates", json=command, headers={"Idempotency-Key": key})
        ).json() == saved.json()
    row = (
        await harness.sql(
            "SELECT payload FROM warning_acknowledgments WHERE organization_id=:org",
            {"org": harness.actor.organization_id},
        )
    ).scalar_one()
    assert row["assessment"]["id"] == a["id"] and row["assessment"]["result"]["score"] == "0"
    assert row["assessment"]["warnings"] == a["warnings"]


@pytest.mark.asyncio
async def test_latest_assessment_edit_and_foreign_access_cannot_reuse_ack(harness: Harness):
    task = await seed_task(harness)
    app = assessed_app(harness)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        proof = (await upload(c)).json()
        refs = [{"evidence_id": proof["evidence_id"], "version": 1}]
        d = await draft(c, [body(task, evidence_refs=refs)])
        old = await assess(c, d)
        new = await assess(c, d)
        assert old["id"] != new["id"]
        command = {
            "draft_id": d["id"],
            "expected_draft_version": 1,
            "assessment_id": old["id"],
            "warning_acknowledgments": [w["id"] for w in old["warnings"]],
        }
        response = await c.post(
            "/api/v1/daily-updates", json=command, headers={"Idempotency-Key": str(uuid4())}
        )
        assert response.status_code == 409
        app.dependency_overrides[get_authenticated_actor] = lambda: harness.foreign
        assert (
            await c.get(f"/api/v1/daily-updates/{d['id']}/evidence-assessments")
        ).status_code == 404
        response = await c.post(
            f"/api/v1/daily-updates/{d['id']}/assess",
            json={"expected_version": 1},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 404
        app.dependency_overrides[get_authenticated_actor] = lambda: harness.actor
        revised = await c.patch(
            f"/api/v1/daily-updates/{d['id']}/draft",
            json={
                "expected_version": 1,
                "items": [body(task, done_text="Revised", evidence_refs=refs)],
            },
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert revised.status_code == 200
        assert (await c.get(f"/api/v1/daily-updates/{d['id']}/evidence-assessments")).json()[
            "id"
        ] is None
        command["expected_draft_version"] = 2
        assert (
            await c.post(
                "/api/v1/daily-updates", json=command, headers={"Idempotency-Key": str(uuid4())}
            )
        ).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), ValueError("invalid structured output")])
async def test_failure_is_server_owned_manual_fallback(
    harness: Harness, failure: Exception
) -> None:
    task = await seed_task(harness)
    app = assessed_app(harness, Comparison(failure=failure))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        proof = (await upload(c)).json()
        d = await draft(
            c, [body(task, evidence_refs=[{"evidence_id": proof["evidence_id"], "version": 1}])]
        )
        a = await assess(c, d)
        assert a["state"] == "UNAVAILABLE" and a["result"] is None
        command = {"draft_id": d["id"], "expected_draft_version": 1, "assessment_id": a["id"]}
        response = await c.post(
            "/api/v1/daily-updates", json=command, headers={"Idempotency-Key": str(uuid4())}
        )
        assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_changed_source_between_preview_and_submit_is_rejected(harness: Harness) -> None:
    task = await seed_task(harness)
    app = assessed_app(harness)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        proof = (await upload(c)).json()
        d = await draft(
            c, [body(task, evidence_refs=[{"evidence_id": proof["evidence_id"], "version": 1}])]
        )
        a = await assess(c, d)
        await harness.sql(
            "UPDATE evidence_originals SET state='EXPIRED' WHERE id=:id",
            {"id": proof["evidence_id"]},
        )
        response = await c.post(
            "/api/v1/daily-updates",
            json={
                "draft_id": d["id"],
                "expected_draft_version": 1,
                "assessment_id": a["id"],
                "warning_acknowledgments": [w["id"] for w in a["warnings"]],
            },
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 422
        assert (
            await harness.sql(
                "SELECT count(*) FROM daily_updates WHERE organization_id=:org",
                {"org": harness.actor.organization_id},
            )
        ).scalar_one() == 0
        assert (
            await c.get(f"/api/v1/daily-updates/{d['id']}/evidence-assessments")
        ).status_code == 422


@pytest.mark.asyncio
async def test_new_terminal_assessment_invalidates_other_tab_confirmation(harness: Harness) -> None:
    import asyncio

    from app.modules.progress.application.assessment_service import AssessmentService

    task = await seed_task(harness)
    app = assessed_app(harness)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        proof = (await upload(c)).json()
        d = await draft(
            c, [body(task, evidence_refs=[{"evidence_id": proof["evidence_id"], "version": 1}])]
        )
        old = await assess(c, d)
        entered, release = asyncio.Event(), asyncio.Event()

        class Delayed(Comparison):
            async def compare(
                self,
                claims: tuple[Claim, ...],
                original_sources: tuple[OriginalSource, ...],
                budget: ComparisonBudget,
            ) -> EvidenceComparisonResult:
                entered.set()
                await release.wait()
                return await super().compare(claims, original_sources, budget)

        service: AssessmentService = app.state.assessment_service
        service.comparison = Delayed("SUPPORTED")
        running = asyncio.create_task(
            service.assess(harness.actor, UUID(d["id"]), 1, str(uuid4()), "test-concurrent")
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            pending = (await c.get(f"/api/v1/daily-updates/{d['id']}/evidence-assessments")).json()
            assert pending["state"] == "PENDING"
            command = {
                "draft_id": d["id"],
                "expected_draft_version": 1,
                "assessment_id": old["id"],
                "warning_acknowledgments": [w["id"] for w in old["warnings"]],
            }
            response = await c.post(
                "/api/v1/daily-updates", json=command, headers={"Idempotency-Key": str(uuid4())}
            )
            assert (
                response.status_code == 409
                and response.json()["error"]["code"] == "ASSESSMENT_PENDING"
            )
        finally:
            release.set()
            await running
        latest = (await c.get(f"/api/v1/daily-updates/{d['id']}/evidence-assessments")).json()
        assert latest["id"] != old["id"] and latest["result"]["score"] == "100"
        assert (
            await c.post(
                "/api/v1/daily-updates", json=command, headers={"Idempotency-Key": str(uuid4())}
            )
        ).status_code == 409
        command.update(assessment_id=latest["id"], warning_acknowledgments=[])
        assert (
            await c.post(
                "/api/v1/daily-updates", json=command, headers={"Idempotency-Key": str(uuid4())}
            )
        ).status_code == 201
        assert (
            await c.post(
                "/api/v1/daily-updates", json=command, headers={"Idempotency-Key": str(uuid4())}
            )
        ).status_code == 409


@pytest.mark.asyncio
async def test_criteria_versions_and_acknowledgment_history_are_immutable(harness: Harness) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    task = await seed_task(harness)
    criterion = uuid4()
    await harness.sql(
        "INSERT INTO acceptance_criteria(id,organization_id,task_id,text,position,version,"
        "created_by_membership_id,updated_by_membership_id) "
        "VALUES(:id,:org,:task,'Delivered',1,1,:member,:member)",
        {
            "id": criterion,
            "org": harness.actor.organization_id,
            "task": task,
            "member": harness.peer.membership_id,
        },
    )
    app = assessed_app(harness)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        proof = (await upload(c)).json()
        d = await draft(
            c, [body(task, evidence_refs=[{"evidence_id": proof["evidence_id"], "version": 1}])]
        )
        a = await assess(c, d)
        await harness.sql(
            "UPDATE acceptance_criteria SET version=2 WHERE id=:id", {"id": criterion}
        )
        current = (await c.get(f"/api/v1/daily-updates/{d['id']}/evidence-assessments")).json()
        assert current["state"] == "STALE" and current["result"] is None
        command = {
            "draft_id": d["id"],
            "expected_draft_version": 1,
            "assessment_id": a["id"],
            "warning_acknowledgments": [w["id"] for w in a["warnings"]],
        }
        response = await c.post(
            "/api/v1/daily-updates", json=command, headers={"Idempotency-Key": str(uuid4())}
        )
        assert (
            response.status_code == 409 and response.json()["error"]["code"] == "STALE_ASSESSMENT"
        )
        assert response.json()["error"]["details"]["assessment"]["state"] == "STALE"
        a = await assess(c, d)
        command.update(
            assessment_id=a["id"], warning_acknowledgments=[w["id"] for w in a["warnings"]]
        )
        assert (
            await c.post(
                "/api/v1/daily-updates", json=command, headers={"Idempotency-Key": str(uuid4())}
            )
        ).status_code == 201
    for table in ("evidence_assessments", "warning_acknowledgments"):
        for actor, count in (
            (harness.actor, 1 if table == "warning_acknowledgments" else 2),
            (harness.peer, 0),
            (harness.foreign, 0),
        ):
            await harness.sql("SET LOCAL ROLE app_runtime")
            await harness.connection.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true), "
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            assert (
                await harness.connection.execute(text(f"SELECT count(*) FROM {table}"))
            ).scalar_one() == count
        await harness.sql("RESET ROLE")
        for sql in (
            f"UPDATE {table} SET payload='{{}}'::jsonb WHERE organization_id=:org",
            f"DELETE FROM {table} WHERE organization_id=:org",
        ):
            with pytest.raises(DBAPIError):
                async with harness.connection.begin_nested():
                    await harness.connection.execute(
                        text(sql), {"org": harness.actor.organization_id}
                    )
    assert (
        await harness.sql(
            "SELECT count(*) FROM outbox_events WHERE organization_id=:org "
            "AND event_type='daily_update.assessment_finished'",
            {"org": harness.actor.organization_id},
        )
    ).scalar_one() == 2


@pytest.mark.asyncio
async def test_active_comparison_cannot_be_skipped_with_an_identical_new_draft(
    harness: Harness,
) -> None:
    task = await seed_task(harness)
    app = assessed_app(harness)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        proof = (await upload(c)).json()
        items = [body(task, evidence_refs=[{"evidence_id": proof["evidence_id"], "version": 1}])]
        old = await draft(c, items)
        await assess(c, old)
        new = await draft(c, items)
        response = await c.post(
            "/api/v1/daily-updates",
            json={"draft_id": new["id"], "expected_draft_version": 1},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert (
            response.status_code == 409
            and response.json()["error"]["code"] == "ASSESSMENT_REQUIRED"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["valid", "forged", "duplicate"])
async def test_partial_source_receipts_produce_insufficient_assessment(
    harness: Harness, tamper: str
) -> None:
    from app.modules.progress.domain.daily_updates import SelectedEvidence

    task = await seed_task(harness)

    class Partial(Comparison):
        async def compare(
            self,
            claims: tuple[Claim, ...],
            original_sources: tuple[OriginalSource, ...],
            budget: ComparisonBudget,
        ) -> EvidenceComparisonResult:
            findings = await super().compare(claims, original_sources, budget)
            receipt = SelectedEvidence(
                evidence_id=original_sources[0].evidence_id, version=original_sources[0].version
            )
            receipts = (receipt,)
            if tamper == "forged":
                receipts = (SelectedEvidence(evidence_id=uuid4(), version=1),)
            if tamper == "duplicate":
                receipts = (receipt, receipt)
            return EvidenceComparisonResult(
                judgment=findings.judgment,
                findings=findings.findings,
                processed_sources=receipts,
            )

    app = assessed_app(harness, Partial("SUPPORTED"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        first = (await upload(c)).json()
        second = (await upload(c, key="evidence-upload-02")).json()
        d = await draft(
            c,
            [
                body(
                    task,
                    evidence_refs=[
                        {"evidence_id": p["evidence_id"], "version": 1} for p in (first, second)
                    ],
                )
            ],
        )
        a = await assess(c, d)
        if tamper != "valid":
            assert a["state"] == "UNAVAILABLE" and a["result"] is None
            return
        assert a["state"] == "READY"
        assert a["coverage"] == {"processed_count": 1, "total_count": 2}
        assert a["result"]["score"] == "100"
        assert a["result"]["warning_codes"] == ["INSUFFICIENT_ASSESSMENT"]


@pytest.mark.asyncio
async def test_abandoned_attempt_times_out_before_reassessment(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from datetime import UTC, datetime, timedelta

    from app.modules.progress.adapters import assessment_repository
    from app.modules.progress.application.assessment_service import AssessmentService

    task = await seed_task(harness)
    entered = asyncio.Event()

    class Abandoned(Comparison):
        async def compare(
            self,
            claims: tuple[Claim, ...],
            original_sources: tuple[OriginalSource, ...],
            budget: ComparisonBudget,
        ) -> EvidenceComparisonResult:
            entered.set()
            await asyncio.Event().wait()
            return await super().compare(claims, original_sources, budget)

    app = assessed_app(harness, Abandoned())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        proof = (await upload(c)).json()
        d = await draft(
            c, [body(task, evidence_refs=[{"evidence_id": proof["evidence_id"], "version": 1}])]
        )
        service: AssessmentService = app.state.assessment_service
        running = asyncio.create_task(
            service.assess(harness.actor, UUID(d["id"]), 1, str(uuid4()), "abandoned")
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        future = datetime.now(UTC) + timedelta(seconds=61)

        class Future(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                return future

        monkeypatch.setattr(assessment_repository, "datetime", Future)
        service.comparison = Comparison()
        await assess(c, d)
    states = (
        (
            await harness.sql(
                "SELECT state FROM evidence_assessments WHERE draft_id=:draft ORDER BY attempt",
                {"draft": d["id"]},
            )
        )
        .scalars()
        .all()
    )
    assert states == ["UNAVAILABLE", "READY"]
    assert (
        await harness.sql(
            "SELECT count(*) FROM audit_events WHERE organization_id=:org "
            "AND action='daily_update.assessment_timed_out'",
            {"org": harness.actor.organization_id},
        )
    ).scalar_one() == 1


@pytest.mark.asyncio
async def test_ai_score_rationale_and_recommendations_are_persisted(harness: Harness):
    task = await seed_task(harness)
    comparison = Comparison("SUPPORTED")
    comparison.score = Decimal("83")
    app = assessed_app(harness, comparison)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        proof = (await upload(c)).json()
        d = await draft(
            c, [body(task, evidence_refs=[{"evidence_id": proof["evidence_id"], "version": 1}])]
        )
        a = await assess(c, d)
        assert a["result"]["score"] == "83"
        assert a["result"]["scoring_method"] == "AI"
        assert a["result"]["rationale"] == "Mock assessment for this test."
        assert a["result"]["rule_version"] == "evidence-support.ai.v2"
        assert a["warnings"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["101", "NaN", "-1"])
async def test_invalid_ai_score_falls_back_without_fabricated_score(harness: Harness, value: str):
    task = await seed_task(harness)

    class Invalid(Comparison):
        async def compare(
            self,
            claims: tuple[Claim, ...],
            original_sources: tuple[OriginalSource, ...],
            budget: ComparisonBudget,
        ) -> EvidenceComparisonResult:
            result = await super().compare(claims, original_sources, budget)
            return result.model_copy(
                update={"judgment": result.judgment.model_copy(update={"score": Decimal(value)})}
            )

    app = assessed_app(harness, Invalid())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        proof = (await upload(c)).json()
        d = await draft(
            c, [body(task, evidence_refs=[{"evidence_id": proof["evidence_id"], "version": 1}])]
        )
        a = await assess(c, d)
        assert a["state"] == "UNAVAILABLE" and a["result"] is None
        response = await c.post(
            f"/api/v1/daily-updates/{d['id']}/confirm",
            json={"draft_id": d["id"], "expected_draft_version": 1, "assessment_id": a["id"]},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 201, response.text
