"""Orchestrator-owned schedule preparation never bypasses Manager confirmation."""

import os
from typing import Literal
from uuid import UUID, uuid4

import pytest

from app.modules.assistant.adapters.agent_runtime import build_agent_registry
from app.modules.assistant.adapters.automation_tools import AutomationToolAdapter
from app.modules.identity.domain.auth import AuthenticatedActor
from tests.test_daily_summary_schedule_integration import prepared
from tests.test_evidence_api_integration import Harness, harness
from work_management_ai.agents.orchestrator.contracts import (
    ActiveConversationContext,
    OrchestratorInput,
)
from work_management_ai.agents.orchestrator.harness import OrchestratorHarness
from work_management_ai.model_gateway.mock import MockModelGateway
from work_management_ai.runtime.contracts import (
    ActorReference,
    AgentHandoff,
    AgentResult,
    ResolvedActorContext,
)
from work_management_ai.runtime.policy_guard import PolicyGuard

__all__ = ["harness"]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["vi", "en"])
@pytest.mark.parametrize("failure", [None, "invalid", "timeout", "employee", "rejected"])
async def test_chat_previews_schedule_without_creating_business_schedule(
    harness: Harness, locale: Literal["vi", "en"], failure: str | None
):
    _, actor, command, schedules, _ = await prepared(harness)

    class CurrentActors:
        async def resolve(
            self, *, organization_id: UUID, membership_id: UUID
        ) -> AuthenticatedActor:
            return actor

    class Actors:
        async def resolve(self, reference: ActorReference) -> ResolvedActorContext:
            return ResolvedActorContext(
                organization_id=actor.organization_id,
                membership_id=actor.membership_id,
                role="EMPLOYEE" if failure == "employee" else actor.role.value,
                is_active=True,
            )

    class Specialists:
        async def run_specialist(self, handoff: AgentHandoff) -> AgentResult:
            raise AssertionError("No schedule Specialist should be created")

    tools = AutomationToolAdapter(actors=CurrentActors(), schedules=schedules)
    gateway = MockModelGateway(
        fixtures={
            f"orchestrator.schedule.{locale}.v1": {}
            if failure == "invalid"
            else TimeoutError()
            if failure == "timeout"
            else {
                "project_reference": str(uuid4())
                if failure == "rejected"
                else str(command.project_id),
                "operation": "CONFIGURE",
                "timezone": "Asia/Ho_Chi_Minh",
                "cutoff": "17:00",
                "weekdays": [1, 2, 3, 4, 5],
                "recipient_references": ["SELF"],
                "send_when_complete": True,
                "partial_at_cutoff": True,
            }
        }
    )
    orchestrator = OrchestratorHarness(
        model_gateway=gateway,
        registry=build_agent_registry()[0],
        policy_guard=PolicyGuard(),
        actor_resolver=Actors(),
        specialists=Specialists(),
        automation_tools=tools,
    )
    output = await orchestrator.run_turn(
        OrchestratorInput(
            orchestration_run_id=uuid4(),
            conversation_id=uuid4(),
            turn_id=uuid4(),
            actor=ActorReference(
                organization_id=actor.organization_id, membership_id=actor.membership_id
            ),
            locale=locale,
            message=f"Configure daily summary schedule for project {command.project_id}",
            active_context=ActiveConversationContext(recent_messages=()),
        )
    )
    if failure:
        assert not any(b.kind == "daily_summary" for b in output.blocks), output
        assert (await schedules.get(actor, command.project_id)).schedule is None
        assert output.status == "FAILED"
        return
    assert any(b.kind == "daily_summary" for b in output.blocks), output
    block = next(b for b in output.blocks if b.kind == "daily_summary")
    assert block.draft_id is not None
    assert (await schedules.get(actor, command.project_id)).schedule is None
    saved = await schedules.confirm(actor, block.draft_id, block.expected_version, str(uuid4()))
    assert saved.cutoff == "17:00"


@pytest.mark.asyncio
async def test_chat_cutoff_edit_preserves_unspecified_recipients(harness: Harness):
    from app.modules.automations.domain.schedules import ChatScheduleCommand

    _, actor, command, schedules, _ = await prepared(harness)
    command = command.model_copy(
        update={"recipients": (actor.membership_id, harness.actor.membership_id)}
    )
    preview = await schedules.preview(actor, command, 0, str(uuid4()))
    await schedules.confirm(actor, preview.id, 0, str(uuid4()))
    _, draft, _ = await schedules.prepare_from_chat(
        actor,
        ChatScheduleCommand(
            project_reference=str(command.project_id), operation="CONFIGURE", cutoff="18:00"
        ),
        str(uuid4()),
    )
    assert draft is not None and set(draft.command.recipients) == set(command.recipients)


@pytest.mark.asyncio
async def test_chat_empty_weekdays_rejected_and_audited(harness: Harness):
    from app.modules.automations.domain.schedules import ChatScheduleCommand, ScheduleError

    _, actor, command, schedules, _ = await prepared(harness)
    with pytest.raises(ScheduleError):
        await schedules.prepare_from_chat(
            actor,
            ChatScheduleCommand(
                project_reference=str(command.project_id), operation="CONFIGURE", weekdays=()
            ),
            str(uuid4()),
        )
    assert (
        await harness.sql(
            "SELECT count(*) FROM audit_events WHERE organization_id=:org "
            "AND action='schedule.rejected'",
            {"org": actor.organization_id},
        )
    ).scalar_one() == 1


@pytest.mark.asyncio
async def test_schedule_draft_rechecks_current_role_with_structured_error(harness: Harness):
    from app.modules.automations.domain.schedules import ScheduleError

    _, actor, command, schedules, _ = await prepared(harness)
    draft = await schedules.preview(actor, command, 0, str(uuid4()))
    await harness.sql(
        "UPDATE memberships SET role='EMPLOYEE' WHERE id=:id", {"id": actor.membership_id}
    )
    with pytest.raises(ScheduleError):
        await schedules.draft(actor, draft.id)


@pytest.mark.asyncio
async def test_digest_includes_acknowledged_evidence_warning_review(harness: Harness):
    from datetime import UTC, datetime, timedelta

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.modules.automations.adapters.digest_repository import DigestTransactions
    from app.modules.automations.application.digest_service import DigestService
    from app.modules.automations.domain.digests import AuthorizedJobScope
    from tests.test_daily_update_api_integration import body
    from tests.test_daily_update_api_integration import draft as make_draft
    from tests.test_evidence_api_integration import upload
    from tests.test_evidence_assessment_integration import assess, assessed_app

    task, actor, command, schedules, _ = await prepared(harness)
    app = assessed_app(harness)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        proof = (await upload(c)).json()
        d = await make_draft(
            c,
            [body(str(task), evidence_refs=[{"evidence_id": proof["evidence_id"], "version": 1}])],
        )
        assessment = await assess(c, d)
        confirmed = await c.post(
            "/api/v1/daily-updates",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "draft_id": d["id"],
                "expected_draft_version": 1,
                "assessment_id": assessment["id"],
                "warning_acknowledgments": [w["id"] for w in assessment["warnings"]],
            },
        )
        assert confirmed.status_code == 201, confirmed.text
    preview = await schedules.preview(actor, command, 0, str(uuid4()))
    await schedules.confirm(actor, preview.id, 0, str(uuid4()))
    window = (await schedules.get(actor, command.project_id)).window
    assert window is not None
    sessions = async_sessionmaker(
        bind=harness.connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    snapshot = await DigestService(DigestTransactions(sessions)).trigger(
        AuthorizedJobScope(
            actor=actor, at=max(datetime.now(UTC), window.cutoff_at + timedelta(seconds=1))
        ),
        window.id,
        "CUTOFF",
    )
    reviews = [
        s
        for s in snapshot.sources
        if s.kind == "REVIEW" and s.state == "EVIDENCE_WARNING_ACKNOWLEDGED"
    ]
    assert len(reviews) == 1 and reviews[0].task_id == task


@pytest.mark.asyncio
async def test_summary_resources_deny_cross_tenant_access(harness: Harness):
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.modules.automations.adapters.digest_repository import DigestTransactions
    from app.modules.automations.application.digest_service import DigestService
    from app.modules.automations.domain.digests import AuthorizedJobScope
    from app.modules.automations.domain.schedules import ScheduleError
    from app.modules.organization.domain.roles import MembershipRole

    _, actor, command, schedules, _ = await prepared(harness)
    draft = await schedules.preview(actor, command, 0, str(uuid4()))
    await schedules.confirm(actor, draft.id, 0, str(uuid4()))
    window = (await schedules.get(actor, command.project_id)).window
    assert window is not None
    sessions = async_sessionmaker(
        bind=harness.connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    digests = DigestService(DigestTransactions(sessions))
    scope = AuthorizedJobScope(
        actor=actor, at=max(datetime.now(UTC), window.cutoff_at + timedelta(seconds=1))
    )
    await digests.trigger(scope, window.id, "CUTOFF")
    await digests.deliver(scope, worker_id="tenant-test")
    await harness.sql(
        "UPDATE memberships SET role='MANAGER' WHERE id=:id", {"id": harness.foreign.membership_id}
    )
    foreign = replace(harness.foreign, role=MembershipRole.MANAGER)
    assert await digests.list(foreign) == ()
    with pytest.raises(ScheduleError, match="RESOURCE_NOT_FOUND"):
        await digests.trigger(AuthorizedJobScope(actor=foreign, at=scope.at), window.id, "CUTOFF")
    async with DigestTransactions(sessions)(foreign) as repo:
        await repo.authenticate()
        for table in (
            "daily_summary_triggers",
            "daily_summary_snapshots",
            "daily_summary_deliveries",
        ):
            assert await repo.session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
