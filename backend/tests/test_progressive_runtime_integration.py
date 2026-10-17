"""Progressive worker decisions persist without proposal or business side effects."""

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.config import Settings
from app.core.database import create_database_engine, create_session_factory
from app.modules.planning_runs.adapters import ai_runtime
from app.modules.planning_runs.adapters.progressive_runtime import execute_progressive_job
from app.modules.planning_runs.adapters.transaction import PostgreSQLPlanningRunTransactionFactory
from app.modules.planning_runs.domain.models import WorkflowJob, WorkflowJobStatus
from tests.test_planning_draft_api_integration import seed
from work_management_ai.model_gateway.mock import MockModelGateway

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="PostgreSQL opt-in"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["vi", "en"])
@pytest.mark.parametrize("decision", ["ASK_GOAL", "DRAFT", "REPAIRED_INVALID"])
async def test_worker_pauses_for_missing_information_or_publishes_a_verified_draft(
    monkeypatch: pytest.MonkeyPatch, locale: str, decision: str
) -> None:
    settings = Settings(environment="test", progressive_planning_enabled=True)
    engine = create_database_engine(settings)
    transactions = PostgreSQLPlanningRunTransactionFactory(create_session_factory(engine))
    try:
        actor, run_id = await seed(engine)
        async with transactions(actor) as transaction:
            run = await transaction.repository.get_workflow_run(actor=actor, run_id=run_id)
        assert run is not None
        job = WorkflowJob(
            id=uuid4(),
            organization_id=actor.organization_id,
            workflow_run_id=run_id,
            job_type="planning.generate",
            status=WorkflowJobStatus.RUNNING,
            payload={"locale": locale},
        )
        draft = json.loads(
            (Path(__file__).parents[2] / "ai/tests/fixtures/progressive_planning/en.json")
            .read_text()
            .replace("message:1", f"brief:{run_id}")
        )
        draft["brief"]["objective"]["source_refs"] = [f"brief:{run_id}"]
        if decision == "REPAIRED_INVALID":
            # A compact repair leaves room for grounding within the unchanged
            # durable token budget; oversized repairs must fall back safely.
            draft = {
                "brief": {"objective": draft["brief"]["objective"]},
                "duration_days": 7,
                "phases": [{"ref": "p1", "title": "Prepare", "start_day": 0, "end_day": 7}],
                "tasks": [draft["tasks"][0]],
            }
        candidate: dict[str, Any] = (
            {"decision": {"kind": "DRAFT"}}
            if decision == "REPAIRED_INVALID"
            else {
                "decision": {"kind": "DRAFT", "draft": draft, "question": None},
                "change_assessment": None,
            }
            if decision == "DRAFT"
            else {
                "decision": {
                    "kind": "ASK_GOAL",
                    "draft": None,
                    "question": {
                        "id": "goal",
                        "purpose": "GOAL",
                        "text": "Bạn muốn lập kế hoạch cho mục tiêu gì?"
                        if locale == "vi"
                        else "What goal should the plan achieve?",
                        "blocking": True,
                        "decision_impact": None,
                        "source_refs": [],
                    },
                },
                "change_assessment": None,
            }
        )
        gateway = MockModelGateway(
            fixtures={
                f"planning.progressive.{locale}.content": candidate,
                f"planning.progressive.{locale}.grounding": {
                    "passed": True,
                    "unsupported_claim_paths": [],
                    "constraint_violation_paths": [],
                    "safe_reasons": [],
                },
            },
            sequence_fixtures={
                f"planning.progressive.{locale}.content": (
                    candidate,
                    {"decision": {"kind": "DRAFT", "draft": draft, "question": None}},
                )
            }
            if decision == "REPAIRED_INVALID"
            else None,
        )

        def gateway_factory(_: Settings) -> MockModelGateway:
            return gateway

        monkeypatch.setattr(ai_runtime, "build_model_gateway", gateway_factory)
        await execute_progressive_job(
            settings=settings, transactions=transactions, actor=actor, run=run, job=job
        )
        await execute_progressive_job(
            settings=settings, transactions=transactions, actor=actor, run=run, job=job
        )
        async with engine.connect() as connection:
            assert await connection.scalar(
                text("SELECT status FROM workflow_runs WHERE id=:id"), {"id": run_id}
            ) == ("NEEDS_INPUT" if decision == "ASK_GOAL" else "DRAFT_AVAILABLE")
            for table in ("proposals", "projects", "tasks"):
                assert (
                    await connection.scalar(
                        text(f"SELECT count(*) FROM {table} WHERE organization_id=:org"),
                        {"org": actor.organization_id},
                    )
                    == 0
                )
            assert await connection.scalar(
                text("SELECT count(*) FROM model_invocations WHERE workflow_run_id=:id"),
                {"id": run_id},
            ) == (1 if decision == "ASK_GOAL" else 3 if decision == "REPAIRED_INVALID" else 2)
            if decision != "ASK_GOAL":
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM planning_draft_versions "
                            "WHERE organization_id=:org"
                        ),
                        {"org": actor.organization_id},
                    )
                    == 1
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["revoked", "invalid"])
async def test_revocation_and_retries_never_publish_or_reset_attempts(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from pydantic import BaseModel

    from work_management_ai.model_gateway.contracts import (
        StructuredModelRequest,
        StructuredModelResponse,
    )
    from work_management_ai.model_gateway.errors import ModelInvalidOutputError

    settings = Settings(environment="test", progressive_planning_enabled=True)
    engine = create_database_engine(settings)
    transactions = PostgreSQLPlanningRunTransactionFactory(create_session_factory(engine))
    try:
        actor, run_id = await seed(engine)
        async with transactions(actor) as transaction:
            run = await transaction.repository.get_workflow_run(actor=actor, run_id=run_id)
        assert run is not None
        job = WorkflowJob(
            id=uuid4(),
            organization_id=actor.organization_id,
            workflow_run_id=run_id,
            job_type="planning.generate",
            status=WorkflowJobStatus.RUNNING,
            payload={"locale": "en"},
        )
        content = json.loads(
            (
                Path(__file__).parents[2] / "ai/tests/fixtures/progressive_planning/en.json"
            ).read_text()
        )
        content["brief"]["objective"]["source_refs"] = [f"brief:{run_id}"]
        fixture = MockModelGateway(
            fixtures={
                "planning.progressive.en.content": {
                    "decision": {"kind": "DRAFT", "draft": content, "question": None},
                    "change_assessment": None,
                },
                "planning.progressive.en.grounding": {
                    "passed": True,
                    "unsupported_claim_paths": [],
                    "constraint_violation_paths": [],
                    "safe_reasons": [],
                },
            }
        )

        class Gateway:
            calls = 0

            async def generate_structured[T: BaseModel](
                self, request: StructuredModelRequest[T]
            ) -> StructuredModelResponse[T]:
                self.calls += 1
                if failure == "invalid":
                    raise ModelInvalidOutputError("synthetic invalid output")
                result = await fixture.generate_structured(request)
                if self.calls == 2:
                    async with engine.begin() as connection:
                        await connection.execute(
                            text("UPDATE memberships SET is_active=false WHERE id=:id"),
                            {"id": actor.membership_id},
                        )
                return result

        gateway = Gateway()

        def gateway_factory(_: Settings) -> Gateway:
            return gateway

        monkeypatch.setattr(ai_runtime, "build_model_gateway", gateway_factory)
        await execute_progressive_job(
            settings=settings, transactions=transactions, actor=actor, run=run, job=job
        )
        if failure == "invalid":
            await execute_progressive_job(
                settings=settings, transactions=transactions, actor=actor, run=run, job=job
            )
            assert (
                gateway.calls == 2
            )  # Retry retains the consumed attempts and output reservations.
        async with engine.connect() as connection:
            for table in ("planning_drafts", "proposals", "projects", "tasks"):
                assert (
                    await connection.scalar(
                        text(f"SELECT count(*) FROM {table} WHERE organization_id=:org"),
                        {"org": actor.organization_id},
                    )
                    == 0
                )
            assert (
                await connection.scalar(
                    text("SELECT status FROM workflow_runs WHERE id=:id"), {"id": run_id}
                )
                == "FAILED"
            )
    finally:
        await engine.dispose()
