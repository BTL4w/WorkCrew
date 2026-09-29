"""Separate committed connections exercise real row and actor/date locks."""

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import Settings
from app.core.database import create_database_engine, create_session_factory
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.progress.adapters.daily_update_repository import SqlAlchemyDailyUpdateTransactions
from app.modules.progress.application.daily_update_service import DailyUpdateService
from app.modules.progress.domain.daily_updates import (
    ConfirmDailyUpdateCommand,
    DailyUpdateError,
    DailyUpdateItemInput,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


@dataclass
class Case:
    service: DailyUpdateService
    actor: AuthenticatedActor
    tasks: tuple[UUID, UUID]


@pytest_asyncio.fixture
async def case():
    engine = create_database_engine(Settings(environment="test"))
    org, user, member = uuid4(), uuid4(), uuid4()
    tasks = (uuid4(), uuid4())
    # Dedicated integration database; random tenant isolates committed race fixtures.
    async with engine.begin() as c:
        await c.execute(
            text("INSERT INTO organizations(id,slug,name) VALUES (:id,:slug,'Reporting race')"),
            {"id": org, "slug": str(org)},
        )
        await c.execute(
            text(
                "INSERT INTO "
                "users(id,email_normalized,email_display,display_name,password_hash) "
                "VALUES (:id,:email,:email,'Reporter','unused')"
            ),
            {"id": user, "email": f"{user}@example.test"},
        )
        await c.execute(
            text(
                "INSERT INTO memberships(id,organization_id,user_id,role) VALUES "
                "(:id,:org,:user,'EMPLOYEE')"
            ),
            {"id": member, "org": org, "user": user},
        )
        for task in tasks:
            project = uuid4()
            await c.execute(
                text(
                    "INSERT INTO "
                    "projects(id,organization_id,name,created_by_membership_id,"
                    "updated_by_membership_id) "
                    "VALUES (:id,:org,'Report',:m,:m)"
                ),
                {"id": project, "org": org, "m": member},
            )
            await c.execute(
                text(
                    "INSERT INTO "
                    "tasks(id,organization_id,project_id,title,status,required_skill_labels,"
                    "assignee_membership_id,created_by_membership_id,updated_by_membership_id) "
                    "VALUES (:id,:org,:p,'Report','IN_PROGRESS','[]',:m,:m,:m)"
                ),
                {"id": task, "org": org, "p": project, "m": member},
            )
    actor = AuthenticatedActor(
        user,
        f"{user}@example.test",
        "Reporter",
        member,
        org,
        "Reporting race",
        MembershipRole.EMPLOYEE,
    )
    yield Case(
        DailyUpdateService(SqlAlchemyDailyUpdateTransactions(create_session_factory(engine))),
        actor,
        tasks,
    )
    await engine.dispose()


def item(task: UUID, **changes: object) -> DailyUpdateItemInput:
    return DailyUpdateItemInput.model_validate(
        {
            "task_id": task,
            "expected_task_version": 1,
            "expected_progress_version": 0,
            "reported_percent": "50",
            "spent_hours": "3",
            "reporting_date": datetime.now(UTC).date(),
            "done_text": "Prepared",
            **changes,
        }
    )


async def submit(case: Case, draft_id: UUID):
    try:
        result = await case.service.confirm(
            case.actor,
            ConfirmDailyUpdateCommand(draft_id=draft_id, expected_draft_version=1),
            str(uuid4()),
            "race",
        )
        return 201, result
    except DailyUpdateError as e:
        return e.status, e.code


@pytest.mark.asyncio
async def test_confirmation_race_and_worklog_correction(case: Case):
    drafts = [
        await case.service.create_draft(case.actor, (item(case.tasks[0]),), str(uuid4()), "race")
        for _ in range(2)
    ]
    results = await asyncio.gather(*(submit(case, d.id) for d in drafts))
    assert sorted(status for status, _ in results) == [201, 409]
    history = await case.service.history(case.actor, case.tasks[0])
    assert len(history) == 1
    corrected = await case.service.create_draft(
        case.actor,
        (
            item(
                case.tasks[0],
                expected_progress_version=1,
                spent_hours="2",
                corrects_observation_id=history[0].id,
                correction_reason="Correct hours",
            ),
        ),
        str(uuid4()),
        "correct",
    )
    assert (await submit(case, corrected.id))[0] == 201
    engine = create_database_engine(Settings(environment="test"))
    async with engine.connect() as c:
        total = await c.scalar(
            text(
                "SELECT sum(w.spent_hours) FROM work_logs w WHERE "
                "w.organization_id=:org AND NOT EXISTS (SELECT 1 FROM "
                "task_progress_observations o WHERE "
                "o.organization_id=w.organization_id AND "
                "o.corrects_observation_id=w.observation_id)"
            ),
            {"org": case.actor.organization_id},
        )
        assert total == Decimal("2")
        assert (
            await c.scalar(text("SELECT status FROM tasks WHERE id=:id"), {"id": case.tasks[0]})
            == "IN_PROGRESS"
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_different_tasks_enforce_actor_date_limit(case: Case):
    drafts = [
        await case.service.create_draft(
            case.actor, (item(task, spent_hours="15"),), str(uuid4()), "hours"
        )
        for task in case.tasks
    ]
    results = await asyncio.gather(*(submit(case, d.id) for d in drafts))
    assert sorted(status for status, _ in results) == [201, 422]
    assert any(result == "DAILY_HOURS_LIMIT" for _, result in results)
    assert sum([len(await case.service.history(case.actor, task)) for task in case.tasks]) == 1


@pytest.mark.asyncio
async def test_backdated_report_never_displaces_newer_actuals(case: Case):
    from datetime import timedelta

    latest = await case.service.create_draft(
        case.actor, (item(case.tasks[0], reported_percent="70"),), str(uuid4()), "latest"
    )
    assert (await submit(case, latest.id))[0] == 201
    backdated = await case.service.create_draft(
        case.actor,
        (
            item(
                case.tasks[0],
                expected_progress_version=1,
                reporting_date=datetime.now(UTC).date() - timedelta(days=1),
                reported_percent="80",
            ),
        ),
        str(uuid4()),
        "backdated",
    )
    assert (await submit(case, backdated.id))[0] == 201
    context = await case.service.context(case.actor, case.tasks[0])
    assert context.reported_percent == 70
    assert context.progress_version == 2
