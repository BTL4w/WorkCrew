"""Phase4 tool authority and durable recovery cannot cross tenant boundaries."""

import os
from dataclasses import replace
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.assistant.adapters.daily_update_tools import DailyUpdateToolAdapter
from app.modules.assistant.adapters.daily_update_usage import SqlAlchemyDailyUsageStore
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.progress.application.assessment_service import AssessmentService
from tests.test_daily_update_api_integration import daily_app, seed_task
from tests.test_evidence_api_integration import Harness, harness
from work_management_ai.runtime.contracts import ActorReference, ToolExecutionRequest
from work_management_ai.runtime.daily_update_budget import BudgetScope, UsageLimitExceeded

__all__ = ["harness"]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


@pytest.mark.asyncio
async def test_daily_tool_rejects_resolved_actor_scope_mismatch(harness: Harness):
    foreign_task = await seed_task(replace(harness, actor=harness.foreign, peer=harness.foreign))
    app = daily_app(harness)

    class WrongActor:
        async def resolve(
            self, *, organization_id: UUID, membership_id: UUID
        ) -> AuthenticatedActor:
            return harness.foreign

    tools = DailyUpdateToolAdapter(
        actors=WrongActor(),
        updates=app.state.daily_update_service,
        assessments=cast(AssessmentService, object()),
    )
    result = await tools.execute(
        ToolExecutionRequest(
            agent_run_id=uuid4(),
            tool_id="daily_update.prepare",
            tool_version="1.0.0",
            call_id=str(uuid4()),
            idempotency_key=str(uuid4()),
            actor=ActorReference(
                organization_id=harness.actor.organization_id,
                membership_id=harness.actor.membership_id,
            ),
            typed_input={
                "action": "CONTEXT",
                "task_id": foreign_task,
                "task_version": 1,
                "evidence_refs": [],
            },
        )
    )
    assert result.status == "REJECTED", result
    assert "task_id" not in result.typed_output


@pytest.mark.asyncio
async def test_recovered_run_and_new_run_share_original_version_budget(harness: Harness):
    sessions = async_sessionmaker(
        bind=harness.connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    from httpx import ASGITransport, AsyncClient

    from tests.test_evidence_api_integration import upload

    async with AsyncClient(
        transport=ASGITransport(app=harness.app()), base_url="http://test"
    ) as client:
        proof = (await upload(client)).json()
    evidence = UUID(proof["evidence_id"])
    scope = BudgetScope(
        organization_id=harness.actor.organization_id,
        membership_id=harness.actor.membership_id,
        run_id=uuid4(),
        evidence_versions=((evidence, 1),),
    )
    for _ in range(3):
        await SqlAlchemyDailyUsageStore(sessions).reserve(scope, input_tokens=10, output_tokens=10)
    with pytest.raises(UsageLimitExceeded):
        await SqlAlchemyDailyUsageStore(sessions).reserve(
            replace(scope), input_tokens=10, output_tokens=10
        )
    # Changing operational run identity does not reset a version's read budget.
    for _ in range(7):
        await SqlAlchemyDailyUsageStore(sessions).reserve(
            replace(scope, run_id=uuid4()), input_tokens=10, output_tokens=10
        )
    with pytest.raises(UsageLimitExceeded):
        await SqlAlchemyDailyUsageStore(sessions).reserve(
            replace(scope, run_id=uuid4()), input_tokens=10, output_tokens=10
        )
