"""Approval binds an immutable draft and never applies stale or duplicate graphs."""

import json
import os
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.config import Settings
from app.core.database import create_database_engine, create_session_factory
from app.modules.planning_runs.adapters.ai_runtime import PlanningAIRuntime
from app.modules.planning_runs.adapters.draft_repository import build_draft_transaction_factory
from app.modules.planning_runs.adapters.transaction import PostgreSQLPlanningRunTransactionFactory
from app.modules.planning_runs.application.approval_ports import ApprovalDecision
from app.modules.planning_runs.application.approval_service import ApprovalService
from app.modules.planning_runs.application.draft_service import PlanningDraftService
from app.modules.planning_runs.domain.drafts import DraftContent
from app.modules.planning_runs.domain.models import PlanningRunDomainError
from tests.test_planning_draft_api_integration import seed

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="PostgreSQL opt-in"),
]


@pytest.mark.asyncio
async def test_prepare_update_stale_confirm_and_latest_approval_replay() -> None:
    engine = create_database_engine(Settings(environment="test"))
    transactions = PostgreSQLPlanningRunTransactionFactory(create_session_factory(engine))
    runtime = PlanningAIRuntime()
    service = PlanningDraftService(
        transaction_factory=build_draft_transaction_factory(transactions, runtime=runtime)
    )
    approvals = ApprovalService(transaction_factory=transactions, runtime=runtime)
    try:
        actor, run_id = await seed(engine)
        content = json.loads(
            (
                Path(__file__).parents[2] / "ai/tests/fixtures/progressive_planning/en.json"
            ).read_text()
        )
        original = await service.publish_version(
            actor=actor,
            run_id=run_id,
            expected_draft_version=None,
            draft=DraftContent(content=content),
            request_id="draft",
            idempotency_key="draft",
        )
        prepared = await service.prepare_action(
            actor=actor,
            draft_id=original.draft_id,
            expected_draft_version=1,
            anchor_date=date.today() + timedelta(days=7),
            request_id="prepare",
            idempotency_key="prepare",
        )
        replay = await service.prepare_action(
            actor=actor,
            draft_id=original.draft_id,
            expected_draft_version=1,
            anchor_date=date.today() + timedelta(days=7),
            request_id="prepare",
            idempotency_key="prepare",
        )
        assert prepared == replay
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM projects WHERE organization_id=:org"),
                    {"org": actor.organization_id},
                )
                == 0
            )
        content["brief"]["budget"] = {
            "amount": 10000000,
            "currency": "VND",
            "status": "USER_STATED",
            "source_refs": ["message:2"],
        }
        changed = await service.publish_version(
            actor=actor,
            run_id=run_id,
            expected_draft_version=1,
            draft=DraftContent(content=content),
            request_id="edit",
            idempotency_key="edit",
        )
        with pytest.raises(PlanningRunDomainError):
            await approvals.decide(
                actor=actor,
                approval_id=prepared.approval_id,
                decision=ApprovalDecision.APPROVE,
                expected_proposal_version=prepared.proposal_version,
                reason=None,
                request_id="stale",
                idempotency_key="stale",
            )
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM projects WHERE organization_id=:org"),
                    {"org": actor.organization_id},
                )
                == 0
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM audit_events "
                        "WHERE organization_id=:org AND outcome='REJECTED'"
                    ),
                    {"org": actor.organization_id},
                )
                >= 1
            )
        latest = await service.prepare_action(
            actor=actor,
            draft_id=changed.draft_id,
            expected_draft_version=2,
            anchor_date=date.today() + timedelta(days=7),
            request_id="latest",
            idempotency_key="latest",
        )
        assert latest.proposal_id == prepared.proposal_id and latest.proposal_version == 2
        result = await approvals.decide(
            actor=actor,
            approval_id=latest.approval_id,
            decision=ApprovalDecision.APPROVE,
            expected_proposal_version=2,
            reason=None,
            request_id="confirm",
            idempotency_key="confirm",
        )
        duplicate = await approvals.decide(
            actor=actor,
            approval_id=latest.approval_id,
            decision=ApprovalDecision.APPROVE,
            expected_proposal_version=2,
            reason=None,
            request_id="confirm",
            idempotency_key="confirm",
        )
        assert duplicate.created.project_id == result.created.project_id
        terminal = await service.get_snapshot(actor=actor, draft_id=changed.draft_id)
        assert terminal.can_prepare is False
        from app.modules.assistant.adapters.planning_projection import PlanningBlockProjector

        projected = await PlanningBlockProjector(drafts=service, transactions=transactions).project(
            actor,
            {
                "kind": "proposal",
                "workflow_run_id": str(run_id),
                "proposal_id": str(prepared.proposal_id),
                "proposal_version": 1,
                "approval_id": str(prepared.approval_id),
            },
        )
        assert projected["read_only"] is True and projected["can_approve"] is False
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM projects WHERE organization_id=:org"),
                    {"org": actor.organization_id},
                )
                == 1
            )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM tasks WHERE organization_id=:org"),
                    {"org": actor.organization_id},
                )
                == 1
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT source_draft_version FROM proposal_versions "
                        "WHERE proposal_id=:id AND version_number=2"
                    ),
                    {"id": latest.proposal_id},
                )
                == 2
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_draft_update_and_approval_cannot_apply_outdated_graph() -> None:
    import asyncio
    from dataclasses import replace

    engine = create_database_engine(Settings(environment="test"))
    transactions = PostgreSQLPlanningRunTransactionFactory(create_session_factory(engine))
    runtime = PlanningAIRuntime()
    service = PlanningDraftService(
        transaction_factory=build_draft_transaction_factory(transactions, runtime=runtime)
    )
    approvals = ApprovalService(transaction_factory=transactions, runtime=runtime)
    try:
        actor, run_id = await seed(engine)
        content = DraftContent(
            content=json.loads(
                (
                    Path(__file__).parents[2] / "ai/tests/fixtures/progressive_planning/en.json"
                ).read_text()
            )
        )
        draft = await service.publish_version(
            actor=actor,
            run_id=run_id,
            expected_draft_version=None,
            draft=content,
            request_id="first",
            idempotency_key="first",
        )
        prepared = await service.prepare_action(
            actor=actor,
            draft_id=draft.draft_id,
            expected_draft_version=1,
            anchor_date=date.today() + timedelta(days=7),
            request_id="prepare",
            idempotency_key="prepare",
        )
        update, confirm = await asyncio.gather(
            service.publish_version(
                actor=actor,
                run_id=run_id,
                expected_draft_version=1,
                draft=replace(content, change_summary="Changed draft"),
                request_id="race-edit",
                idempotency_key="race-edit",
            ),
            approvals.decide(
                actor=actor,
                approval_id=prepared.approval_id,
                decision=ApprovalDecision.APPROVE,
                expected_proposal_version=1,
                reason=None,
                request_id="race-confirm",
                idempotency_key="race-confirm",
            ),
            return_exceptions=True,
        )
        assert isinstance(update, PlanningRunDomainError) != isinstance(
            confirm, PlanningRunDomainError
        )
        applied = not isinstance(confirm, BaseException)
        latest = await service.get_snapshot(actor=actor, draft_id=draft.draft_id)
        assert latest.current_version == (1 if applied else 2)
        async with engine.connect() as connection:
            for table in ("projects", "tasks"):
                assert await connection.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE organization_id=:org"),
                    {"org": actor.organization_id},
                ) == int(applied)
    finally:
        await engine.dispose()
