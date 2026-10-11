"""Measured database evidence for the three language-independent acceptance boundaries."""

import json
import os
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.database import create_database_engine, create_session_factory
from app.modules.planning_runs.adapters.draft_repository import build_draft_transaction_factory
from app.modules.planning_runs.adapters.transaction import PostgreSQLPlanningRunTransactionFactory
from app.modules.planning_runs.application.draft_service import PlanningDraftService
from app.modules.planning_runs.domain.drafts import DraftContent
from tests.test_planning_draft_api_integration import seed
from tests.test_planning_draft_api_integration import (
    test_draft_publish_race_replay_rls_and_revoked_read as verify_permission_boundary,
)
from tests.test_planning_draft_approval_integration import (
    test_prepare_update_stale_confirm_and_latest_approval_replay as verify_stale_boundary,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="PostgreSQL opt-in"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["vi", "en"])
async def test_refresh_boundary(locale: str) -> None:
    engine = create_database_engine(Settings(environment="test"))
    transactions = PostgreSQLPlanningRunTransactionFactory(create_session_factory(engine))
    service = PlanningDraftService(
        transaction_factory=build_draft_transaction_factory(transactions)
    )
    try:
        actor, run = await seed(engine)
        raw = json.loads(
            (
                Path(__file__).parents[2]
                / f"ai/tests/fixtures/progressive_planning/{locale}-course.json"
            ).read_text()
        )
        first = await service.publish_version(
            actor=actor,
            run_id=run,
            expected_draft_version=None,
            draft=DraftContent(
                content=raw,
                question=raw["open_questions"][0],
                validation={"errors": [], "warnings": [], "conflicts": []},
                versions={"execution_id": "first", "locale": locale},
            ),
            request_id="first",
            idempotency_key="first",
        )
        raw["brief"]["budget"] = {
            "amount": 10000000,
            "currency": "VND",
            "status": "USER_STATED",
            "source_refs": ["message:edit"],
        }
        second = await service.publish_version(
            actor=actor,
            run_id=run,
            expected_draft_version=1,
            draft=DraftContent(
                content=raw,
                question=raw["open_questions"][0],
                validation={"errors": [], "warnings": [], "conflicts": []},
                versions={"execution_id": "second", "locale": locale},
            ),
            request_id="second",
            idempotency_key="second",
        )
        fresh = PlanningDraftService(
            transaction_factory=build_draft_transaction_factory(transactions)
        )
        restored = await fresh.get_snapshot(actor=actor, draft_id=first.draft_id)
        assert restored == second and restored.current_version == 2
        assert restored.content.question == raw["open_questions"][0]
        assert restored.content.content["assumptions"] == raw["assumptions"]
        async with transactions(actor) as transaction:
            checkpoint = await transaction.repository.get_latest_checkpoint(actor=actor, run_id=run)
            workflow = await transaction.repository.get_workflow_run(actor=actor, run_id=run)
        assert checkpoint is not None and checkpoint.state["binding"]["draft_version"] == 2
        assert workflow is not None and workflow.status.value == "DRAFT_AVAILABLE"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["vi", "en"])
async def test_stale_approval_boundary(locale: str) -> None:
    await verify_stale_boundary()


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["vi", "en"])
async def test_permission_boundary(locale: str) -> None:
    await verify_permission_boundary()
