"Synthetic PostgreSQL Assistant scope shared only by retrieval integration tests."

from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.core.database import create_database_engine, create_session_factory
from app.modules.assistant.domain.models import AssistantJob
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole

if TYPE_CHECKING:
    from app.modules.retrieval.application.budget_service import RetrievalBudgetService
    from app.modules.retrieval.domain.state import RetrievalLease

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


@dataclass
class RetrievalHarness:
    engine: AsyncEngine
    actor: AuthenticatedActor
    peer: AuthenticatedActor
    foreign: AuthenticatedActor
    run_id: UUID
    orchestration_id: UUID
    job_id: UUID
    message_id: UUID
    job: AssistantJob
    enabled: bool = True

    async def sql(self, query: str, values: dict[str, Any] | None = None) -> Any:
        async with self.engine.begin() as connection:
            return await connection.execute(text(query), values or {})

    def lease(self, worker: str = "retrieve-test", attempt: int = 1) -> RetrievalLease:
        from app.modules.retrieval.domain.state import RetrievalLease

        return RetrievalLease(self.job_id, worker, attempt)

    def service(self) -> RetrievalBudgetService:
        from app.modules.retrieval.adapters.transaction import (
            PostgreSQLRetrievalTransactions,
            RetrievalActivationPolicy,
        )
        from app.modules.retrieval.application.budget_service import RetrievalBudgetService

        return RetrievalBudgetService(
            transactions=PostgreSQLRetrievalTransactions(create_session_factory(self.engine)),
            activation=RetrievalActivationPolicy(
                lambda: Settings(
                    environment="test",
                    retrieval_enabled=self.enabled,
                    retrieval_provider="mock",
                    retrieval_core_exit_verified=True,
                )
            ),
        )


@pytest_asyncio.fixture
async def retrieval_harness() -> AsyncGenerator[RetrievalHarness]:
    engine = create_database_engine(Settings(environment="test"))
    actors: list[AuthenticatedActor] = []
    organization_id = uuid4()
    async with engine.begin() as connection:
        for index in range(3):
            org = organization_id if index < 2 else uuid4()
            if index != 1:
                await connection.execute(
                    text(
                        "INSERT INTO organizations(id,slug,name) VALUES (:id,:slug,'Retri"
                        "eval test')"
                    ),
                    {"id": org, "slug": f"retrieval-{org}"},
                )
            user, member = uuid4(), uuid4()
            email = f"{user}@example.test"
            await connection.execute(
                text(
                    "INSERT INTO users(id,email_normalized,email_display,display_name,pas"
                    "sword_hash) "
                    "VALUES (:id,:email,:email,'Reader','unused')"
                ),
                {"id": user, "email": email},
            )
            await connection.execute(
                text(
                    "INSERT INTO memberships(id,organization_id,user_id,role) "
                    "VALUES (:id,:org,:user,'EMPLOYEE')"
                ),
                {"id": member, "org": org, "user": user},
            )
            actors.append(
                AuthenticatedActor(
                    user, email, "Reader", member, org, "Test", MembershipRole.EMPLOYEE
                )
            )
        conversation, message, turn, orchestration, job = (uuid4() for _ in range(5))
        parent = uuid5(NAMESPACE_URL, f"orchestrator:{turn}")
        run = uuid5(NAMESPACE_URL, f"agent-run:{turn}:retrieve-1")
        values = {
            "org": organization_id,
            "member": actors[0].membership_id,
            "conversation": conversation,
            "message": message,
            "turn": turn,
            "orchestration": orchestration,
            "parent": parent,
            "run": run,
            "job": job,
        }
        await connection.execute(
            text(
                "INSERT INTO assistant_conversations(id,organization_id,owner_membership_"
                "id,locale) "
                "VALUES (:conversation,:org,:member,'en')"
            ),
            values,
        )
        await connection.execute(
            text(
                "INSERT INTO "
                "assistant_messages(id,organization_id,conversation_id,sequence,role,content_blocks,created_by_membership_id)"
                " VALUES (:message,:org,:conversation,1,'USER','[ "
                '{"kind":"text","text":"Search public web for public planning '
                "at https://example.org/guide\"} ]'::jsonb,:member)"
            ),
            values,
        )
        await connection.execute(
            text(
                "INSERT INTO "
                "assistant_turns(id,organization_id,conversation_id,user_message_id,actor_membership_id,objective,locale,status)"
                " VALUES (:turn,:org,:conversation,:message,:member,'Public "
                "planning','en','RUNNING')"
            ),
            values,
        )
        budget = {
            "max_model_attempts": 3,
            "max_input_tokens": 24000,
            "max_output_tokens": 8000,
            "max_iterations": 16,
            "max_tool_calls": 16,
            "timeout_seconds": 180,
        }
        usage = {
            "model_attempts": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "iterations": 0,
            "tool_calls": 0,
        }
        await connection.execute(
            text(
                "INSERT INTO "
                "orchestration_runs(id,organization_id,turn_id,actor_membership_id,orchestrator_version,orchestrator_fingerprint,execution_plan,budget,usage,status,started_at)"
                " VALUES "
                "(:orchestration,:org,:turn,:member,'1.0.0','test','{}',CAST(:budget"
                " AS jsonb),CAST(:usage AS jsonb),'RUNNING',clock_timestamp())"
            ),
            {**values, "budget": json.dumps(budget), "usage": json.dumps(usage)},
        )
        await connection.execute(
            text(
                "INSERT INTO "
                "agent_runs(id,organization_id,orchestration_run_id,agent_id,agent_version,manifest_fingerprint,capability,typed_input,budget,status,started_at)"
                " VALUES "
                "(:parent,:org,:orchestration,'orchestrator','1.0.0','test','orchestration.delegate','{}',CAST(:budget"
                " AS jsonb),'RUNNING',clock_timestamp())"
            ),
            {**values, "budget": json.dumps(budget)},
        )
        await connection.execute(
            text(
                "INSERT INTO "
                "agent_runs(id,organization_id,orchestration_run_id,parent_agent_run_id,agent_id,agent_version,manifest_fingerprint,capability,typed_input,budget,status,started_at)"
                " VALUES "
                "(:run,:org,:orchestration,:parent,'retrieve','1.0.0',:fingerprint,'retrieval.search_web',CAST(:input"
                " AS jsonb),CAST(:budget AS jsonb),'RUNNING',clock_timestamp())"
            ),
            {
                **values,
                "fingerprint": _manifest_fingerprint(),
                "input": json.dumps(
                    {
                        "query": "public planning",
                        "locale": "en",
                        "requirements": [{"requirement_id": "topic", "description": "Planning"}],
                    }
                ),
                "budget": json.dumps(
                    {
                        **budget,
                        "max_model_attempts": 2,
                        "max_input_tokens": 12000,
                        "max_output_tokens": 3200,
                        "max_iterations": 12,
                        "max_tool_calls": 8,
                        "timeout_seconds": 25,
                    }
                ),
            },
        )
        await connection.execute(
            text(
                "INSERT INTO "
                "assistant_jobs(id,organization_id,conversation_id,turn_id,orchestration_run_id,requester_membership_id,payload,status,attempt_count,locked_by,lease_until)"
                " VALUES "
                "(:job,:org,:conversation,:turn,:orchestration,:member,'{}','RUNNING',1,'retrieve-test',clock_timestamp()+interval"
                " '5 minutes')"
            ),
            values,
        )
    try:
        claim = AssistantJob.create(
            id=job,
            organization_id=organization_id,
            conversation_id=conversation,
            turn_id=turn,
            orchestration_run_id=orchestration,
            requester_membership_id=actors[0].membership_id,
            payload={},
        ).claim(
            worker_id="retrieve-test",
            lease_until=datetime.now(UTC) + timedelta(minutes=5),
            now=datetime.now(UTC),
        )
        yield RetrievalHarness(
            engine, actors[0], actors[1], actors[2], run, orchestration, job, message, claim
        )
    finally:
        await engine.dispose()


def _manifest_fingerprint() -> str:
    from work_management_ai.runtime.manifests import (
        canonical_manifest_fingerprint,
        load_agent_manifest_resource,
    )

    return canonical_manifest_fingerprint(
        load_agent_manifest_resource("work_management_ai.agents.retrieve", "agent.yaml")
    )
