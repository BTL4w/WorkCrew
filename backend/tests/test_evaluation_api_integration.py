from collections.abc import Callable
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.modules.feedback.adapters.evaluation_policy import EvaluationPolicy
from app.modules.feedback.adapters.evaluation_repository import EvaluationTransactions
from app.modules.feedback.application.evaluation_service import EvaluationService
from app.modules.feedback.application.ports import EvaluationProviderPort
from app.modules.identity.api.dependencies import get_authenticated_actor
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from tests.test_report_api_integration import ReportHarness, pytestmark, report_harness
from tests.test_report_evaluation_runner_integration import frozen_dataset

__all__ = ["pytestmark", "report_harness"]


def actor_override(actor: AuthenticatedActor) -> Callable[[], AuthenticatedActor]:
    def resolve() -> AuthenticatedActor:
        return actor

    return resolve


def evaluation_service(
    h: ReportHarness, *, provider: EvaluationProviderPort | None = None
) -> EvaluationService:
    return EvaluationService(
        EvaluationTransactions(async_sessionmaker(h.engine, expire_on_commit=False), "UTC"),
        policy=EvaluationPolicy(Settings(environment="test", ai_provider="mock")),
        provider=provider,
    )


def evaluation_app(
    h: ReportHarness, service: EvaluationService, actor: AuthenticatedActor | None = None
) -> FastAPI:
    app = h.app(actor)
    app.state.evaluation_service = service
    return app


@pytest.mark.asyncio
async def test_eval_api_is_admin_only_and_tenant_bound(report_harness: ReportHarness):
    h = report_harness
    dataset = await frozen_dataset(h)
    service = evaluation_service(h)
    app = evaluation_app(h, service)
    body = {"dataset_version_id": str(dataset.id)}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for actor in (h.employee, replace(h.actor, role=MembershipRole.MANAGER)):
            await h.sql(
                "UPDATE memberships SET role=:role WHERE id=:id",
                {"role": actor.role.value, "id": actor.membership_id},
            )

            app.dependency_overrides[get_authenticated_actor] = actor_override(actor)
            assert (
                await client.post(
                    "/api/v1/evaluations/runs", json=body, headers={"Idempotency-Key": str(uuid4())}
                )
            ).status_code == 403
        await h.sql(
            "UPDATE memberships SET role='ADMIN' WHERE id=:id", {"id": h.actor.membership_id}
        )
        app.dependency_overrides[get_authenticated_actor] = lambda: h.actor
        response = await client.post(
            "/api/v1/evaluations/runs", json=body, headers={"Idempotency-Key": str(uuid4())}
        )
        assert response.status_code == 202
        identity = response.json()["id"]
        assert response.json()["dataset_hash"] == dataset.dataset_hash
        await h.sql(
            "UPDATE memberships SET role='ADMIN' WHERE id=:id", {"id": h.foreign.membership_id}
        )
        app.dependency_overrides[get_authenticated_actor] = lambda: h.foreign
        assert (await client.get(f"/api/v1/evaluations/runs/{identity}")).status_code == 404
        assert (
            await client.post(
                "/api/v1/evaluations/runs", json=body, headers={"Idempotency-Key": str(uuid4())}
            )
        ).status_code == 404


@pytest.mark.asyncio
async def test_eval_start_replays_exact_dataset(report_harness: ReportHarness):
    h = report_harness
    dataset = await frozen_dataset(h)
    app = evaluation_app(h, evaluation_service(h))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        key = str(uuid4())
        body = {"dataset_version_id": str(dataset.id)}
        first = await client.post(
            "/api/v1/evaluations/runs", json=body, headers={"Idempotency-Key": key}
        )
        replay = await client.post(
            "/api/v1/evaluations/runs", json=body, headers={"Idempotency-Key": key}
        )
        assert first.status_code == replay.status_code == 202
        assert replay.json()["id"] == first.json()["id"]
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert (
            await client.post(
                "/api/v1/evaluations/runs",
                json={**body, "provider": "hosted"},
                headers={"Idempotency-Key": key},
            )
        ).status_code == 409
        assert (
            await client.post(
                "/api/v1/evaluations/runs",
                json={"dataset_version_id": str(dataset.cases[0].id)},
                headers={"Idempotency-Key": str(uuid4())},
            )
        ).status_code == 404
        assert (await client.post("/api/v1/evaluations/runs", json=body)).status_code == 422
    rows = await h.sql(
        "SELECT count(*) FROM evaluation_runs WHERE organization_id=:org",
        {"org": h.actor.organization_id},
    )
    assert rows.scalar_one() == 1
    events = await h.sql(
        "SELECT count(*) FROM outbox_events WHERE organization_id=:org AND "
        "event_type='evaluation.run.requested.v1'",
        {"org": h.actor.organization_id},
    )
    assert events.scalar_one() == 1


@pytest.mark.asyncio
async def test_hosted_eval_uses_server_policy(report_harness: ReportHarness):
    h = report_harness
    dataset = await frozen_dataset(h)
    app = evaluation_app(h, evaluation_service(h))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {"dataset_version_id": str(dataset.id), "provider": "hosted"}
        assert (
            await client.post(
                "/api/v1/evaluations/runs", json=body, headers={"Idempotency-Key": str(uuid4())}
            )
        ).status_code == 403
        for field in (
            "budget_tokens",
            "api_key",
            "model",
            "organization_id",
            "policy_version",
            "approved",
        ):
            response = await client.post(
                "/api/v1/evaluations/runs",
                json={**body, field: "spoof"},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert response.status_code == 422
    audit = await h.sql(
        "SELECT count(*) FROM audit_events WHERE organization_id=:org AND "
        "action='evaluation.run.started' AND outcome='REJECTED'",
        {"org": h.actor.organization_id},
    )
    assert audit.scalar_one() >= 7
