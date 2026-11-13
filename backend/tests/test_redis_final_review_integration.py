"""Regressions for final whole-range Redis review."""

import json
import os
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.database import create_session_factory
from app.modules.assistant.adapters.planning_snapshot import PostgreSQLPlanningSnapshot
from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory
from app.modules.assistant.application.service import AssistantService
from app.modules.organization.domain.roles import MembershipRole
from app.modules.planning_runs.adapters.transaction import PostgreSQLPlanningRunTransactionFactory
from app.modules.planning_runs.domain.models import Proposal, ProposalVersion
from tests.retrieval_fixtures import RetrievalHarness, pytestmark, retrieval_harness
from tests.test_planning_run_api_integration import run_for

__all__ = ["pytestmark", "retrieval_harness"]


@pytest.mark.asyncio
async def test_card_validation_completes_with_one_database_connection(
    retrieval_harness: RetrievalHarness,
):
    h = retrieval_harness
    await h.sql("UPDATE memberships SET role='MANAGER' WHERE id=:id", {"id": h.actor.membership_id})
    actor = replace(h.actor, role=MembershipRole.MANAGER)
    engine = create_async_engine(
        os.environ["APP_DATABASE_URL"], pool_size=1, max_overflow=0, pool_timeout=0.1
    )
    sessions = create_session_factory(engine)
    planning = PostgreSQLPlanningRunTransactionFactory(sessions)
    now = datetime.now(UTC)
    run = replace(run_for(actor), created_at=now, updated_at=now)
    proposal = Proposal.create(
        organization_id=actor.organization_id, workflow_run_id=run.id, current_version_number=1
    )
    version = ProposalVersion(
        id=uuid4(),
        organization_id=actor.organization_id,
        proposal_id=proposal.id,
        version_number=1,
        created_by_membership_id=actor.membership_id,
        content={"project": {"title": "Card source"}},
        assumptions=[],
        field_provenance={"default": "AI_PROPOSED"},
    )
    try:
        async with planning(actor) as tx:
            await tx.repository.create_workflow_run(run=run)
            await tx.repository.create_proposal(proposal=proposal, initial_version=version)
        service = AssistantService(
            transaction_factory=PostgreSQLAssistantTransactionFactory(sessions),
            planning_snapshot=PostgreSQLPlanningSnapshot(planning),
            orchestrator_version="1.0.0",
            orchestrator_fingerprint="review-pool",
        )
        key = uuid4().hex

        async def submit():
            return await service.post_message(
                actor=actor,
                conversation_id=h.job.conversation_id,
                message="Revise this plan",
                locale="en",
                card_action={
                    "kind": "PLANNING_REVISE",
                    "proposal_id": str(proposal.id),
                    "workflow_run_id": str(run.id),
                },
                if_match_version=1,
                request_id=uuid4().hex,
                idempotency_key=key,
            )

        accepted = await submit()
        # Accepted exact replay still succeeds after the source version advances.
        async with planning(actor) as tx:
            await tx.repository.append_proposal_version(
                version=replace(version, id=uuid4(), version_number=2)
            )
            await tx.session.execute(
                text(
                    "UPDATE proposals SET current_version_number=2,version=2 "
                    "WHERE organization_id=:org AND id=:id"
                ),
                {"org": actor.organization_id, "id": proposal.id},
            )
        replay = await submit()
        assert replay.replayed and replay.job.id == accepted.job.id
    finally:
        await engine.dispose()


def test_compose_forwards_opt_in_redis_protection_flags_and_limits():
    if shutil.which("docker") is None:
        pytest.skip("requires local Compose CLI")
    root = Path(__file__).resolve().parents[2]
    values = {
        "APP_AI_ADMISSION_ENABLED": "true",
        "APP_AI_ADMISSION_ACTOR_LIMIT": "7",
        "APP_AI_ADMISSION_ORG_LIMIT": "19",
        "APP_RETRIEVAL_ADMISSION_ENABLED": "true",
        "APP_RETRIEVAL_ADMISSION_ACTOR_LIMIT": "3",
        "APP_RETRIEVAL_ADMISSION_ORG_LIMIT": "17",
        "APP_LOGIN_THROTTLE_ENABLED": "true",
        "APP_RATE_LIMIT_HASH_KEY": "synthetic-compose-key-32-bytes-only",
        "APP_REDIS_WAKEUP_ENABLED": "true",
    }
    result = subprocess.run(
        ["docker", "compose", "--env-file", "/dev/null", "config", "--format", "json"],
        cwd=root,
        env=os.environ | values,
        capture_output=True,
        text=True,
        check=True,
    )
    config = json.loads(result.stdout)
    for name in ("backend-api", "ai-worker"):
        environment = config["services"][name]["environment"]
        for field, value in values.items():
            assert environment.get(field) == value, f"{name} did not forward {field}"
