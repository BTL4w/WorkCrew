"""Real draft transactions: immutable history, RLS, races and current permissions."""

import asyncio
import os
from dataclasses import replace
from datetime import datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.core.database import create_database_engine, create_session_factory
from app.main import create_app
from app.modules.identity.api.dependencies import get_authenticated_actor
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.planning_runs.adapters.draft_repository import build_draft_transaction_factory
from app.modules.planning_runs.adapters.transaction import PostgreSQLPlanningRunTransactionFactory
from app.modules.planning_runs.application.draft_service import PlanningDraftService
from app.modules.planning_runs.domain.drafts import DraftContent
from app.modules.planning_runs.domain.models import (
    PlanningRunDomainError,
    ResourceVersionMismatchError,
)
from tests.test_planning_draft_service import manager

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="PostgreSQL integration is opt-in"
    ),
]


async def seed(engine: AsyncEngine) -> tuple[AuthenticatedActor, UUID]:
    actor, run_id = manager(), uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO organizations (id,slug,name) VALUES (:org,:slug,'Draft tenant')"),
            {"org": actor.organization_id, "slug": actor.organization_id.hex},
        )
        await connection.execute(
            text(
                "INSERT INTO users "
                "(id,email_normalized,email_display,display_name,password_hash) VALUES "
                "(:user,:email,:email,'Manager','hash')"
            ),
            {"user": actor.user_id, "email": actor.user_id.hex + "@example.test"},
        )
        await connection.execute(
            text(
                "INSERT INTO memberships (id,organization_id,user_id,role) VALUES "
                "(:member,:org,:user,'MANAGER')"
            ),
            {"member": actor.membership_id, "org": actor.organization_id, "user": actor.user_id},
        )
        await connection.execute(
            text(
                "INSERT INTO workflow_runs "
                "(id,organization_id,requested_by_membership_id,status,workflow_na"
                "me,workflow_version,verifier_version,input_goal_text) "
                "VALUES "
                "(:run,:org,:member,'RUNNING','project_planning','3.0.0','1.0.0','Course "
                "launch')"
            ),
            {"run": run_id, "org": actor.organization_id, "member": actor.membership_id},
        )
    return actor, run_id


@pytest.mark.asyncio
async def test_draft_publish_race_replay_rls_and_revoked_read() -> None:
    engine = create_database_engine(Settings(environment="test"))
    transactions = PostgreSQLPlanningRunTransactionFactory(create_session_factory(engine))
    service = PlanningDraftService(
        transaction_factory=build_draft_transaction_factory(transactions)
    )
    try:
        actor, run = await seed(engine)
        foreign, foreign_run = await seed(engine)
        first = await service.publish_version(
            actor=actor,
            run_id=run,
            expected_draft_version=None,
            draft=DraftContent(content={"objective": "Course"}),
            request_id="first",
            idempotency_key="first",
        )
        replay = await service.publish_version(
            actor=actor,
            run_id=run,
            expected_draft_version=None,
            draft=DraftContent(content={"objective": "Course"}),
            request_id="first",
            idempotency_key="first",
        )
        assert first == replay
        results = await asyncio.gather(
            *(
                service.publish_version(
                    actor=actor,
                    run_id=run,
                    expected_draft_version=1,
                    draft=DraftContent(content={"objective": value}),
                    request_id=value,
                    idempotency_key=value,
                )
                for value in ["Option A", "Option B"]
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(result, ResourceVersionMismatchError) for result in results) == 1
        snapshot = await service.get_snapshot(actor=actor, draft_id=first.draft_id)
        assert snapshot.draft_version == 2 and snapshot.content.content["objective"] in {
            "Option A",
            "Option B",
        }
        assert snapshot.suggested_anchor_date == datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()
        old = await service.get_snapshot(actor=actor, draft_id=first.draft_id, version=1)
        assert old.suggested_anchor_date is None
        assert old.content.content == {"objective": "Course"} and old.current_version == 2
        async with transactions(foreign) as transaction:
            assert (
                await transaction.session.scalar(
                    text("SELECT count(*) FROM planning_drafts WHERE id=:id"),
                    {"id": first.draft_id},
                )
                == 0
            )
            assert (
                await transaction.session.scalar(
                    text("SELECT count(*) FROM planning_draft_versions WHERE draft_id=:id"),
                    {"id": first.draft_id},
                )
                == 0
            )
        with pytest.raises(DBAPIError):
            async with transactions(actor) as transaction:
                await transaction.session.execute(
                    text(
                        "INSERT INTO planning_drafts "
                        "(id,organization_id,workflow_run_id,created_by_membership_id,curr"
                        "ent_version_number) "
                        "VALUES (:id,:org,:run,:owner,1)"
                    ),
                    {
                        "id": uuid4(),
                        "org": foreign.organization_id,
                        "run": foreign_run,
                        "owner": foreign.membership_id,
                    },
                )
        with pytest.raises(DBAPIError):
            async with transactions(actor) as transaction:
                await transaction.session.execute(
                    text(
                        "INSERT INTO planning_drafts "
                        "(id,organization_id,workflow_run_id,created_by_membership_id,curr"
                        "ent_version_number) "
                        "VALUES (:id,:org,:run,:owner,1)"
                    ),
                    {
                        "id": uuid4(),
                        "org": actor.organization_id,
                        "run": foreign_run,
                        "owner": actor.membership_id,
                    },
                )
        with pytest.raises(DBAPIError):
            async with transactions(actor) as transaction:
                await transaction.session.execute(
                    text("UPDATE planning_draft_versions SET content='{}' WHERE draft_id=:id"),
                    {"id": first.draft_id},
                )
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM planning_draft_versions WHERE draft_id=:id"),
                    {"id": first.draft_id},
                )
                == 2
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM workflow_events WHERE workflow_run_id=:id AND "
                        "event_type LIKE 'planning.draft.%'"
                    ),
                    {"id": run},
                )
                == 2
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM audit_events WHERE organization_id=:org AND "
                        "outcome='REJECTED'"
                    ),
                    {"org": actor.organization_id},
                )
                == 1
            )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM projects WHERE organization_id=:org"),
                    {"org": actor.organization_id},
                )
                == 0
            )
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE memberships SET is_active=false WHERE id=:id"),
                {"id": actor.membership_id},
            )
        with pytest.raises(PlanningRunDomainError):
            await service.get_snapshot(actor=actor, draft_id=first.draft_id)
        async with transactions(actor) as transaction:
            assert (
                await transaction.session.scalar(
                    text("SELECT count(*) FROM planning_drafts WHERE id=:id"),
                    {"id": first.draft_id},
                )
                == 0
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_snapshot_endpoint_current_history_and_foreign_scope() -> None:
    settings = Settings(environment="test")
    engine = create_database_engine(settings)
    service = PlanningDraftService(
        transaction_factory=build_draft_transaction_factory(
            PostgreSQLPlanningRunTransactionFactory(create_session_factory(engine))
        )
    )
    try:
        actor, run = await seed(engine)
        snapshot = await service.publish_version(
            actor=actor,
            run_id=run,
            expected_draft_version=None,
            draft=DraftContent(content={"objective": "Course"}),
            request_id="api",
            idempotency_key="api",
        )
        app: FastAPI = create_app(settings=settings, planning_draft_service=service)
        app.dependency_overrides[get_authenticated_actor] = lambda: actor
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/v1/planning-drafts/{snapshot.draft_id}")
            assert response.status_code == 200
            assert response.json()["draft_version"] == 1
            assert response.json()["content"] == {"objective": "Course"}
            response = await client.get(f"/api/v1/planning-drafts/{snapshot.draft_id}/versions/1")
            assert response.status_code == 200
            app.dependency_overrides[get_authenticated_actor] = lambda: replace(
                actor, organization_id=uuid4()
            )
            response = await client.get(f"/api/v1/planning-drafts/{snapshot.draft_id}")
            assert response.status_code == 404
    finally:
        await engine.dispose()
