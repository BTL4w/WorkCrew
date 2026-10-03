"""Different actors and Tasks share originals: confirmation locks cannot cycle."""

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.config import Settings
from app.core.database import create_database_engine, create_session_factory
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.progress.adapters.blocker_repository import (
    SqlAlchemyBlockerTransactions,
)
from app.modules.progress.adapters.daily_update_repository import SqlAlchemyDailyUpdateRepository
from app.modules.progress.application.blocker_service import BlockerService
from app.modules.progress.domain.blockers import BlockerCommand
from app.modules.progress.domain.daily_updates import ConfirmDailyUpdateCommand, SelectedEvidence
from tests.test_daily_update_concurrency_integration import Case, case, item

__all__ = ["case"]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL"),
]


@pytest.mark.asyncio
async def test_report_and_manager_blocker_share_ordered_original_locks(
    case: Case, monkeypatch: pytest.MonkeyPatch
):
    engine = create_database_engine(Settings(environment="test"))
    sessions = create_session_factory(engine)
    actor = case.actor
    user, member = uuid4(), uuid4()
    low, high = sorted((uuid4(), uuid4()))
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO "
                "users(id,email_normalized,email_display,display_name,password_hash) "
                "VALUES (:u,:email,:email,'Manager','unused')"
            ),
            {"u": user, "email": f"{user}@example.test"},
        )
        await connection.execute(
            text(
                "INSERT INTO memberships(id,organization_id,user_id,role) VALUES "
                "(:m,:org,:u,'MANAGER')"
            ),
            {"m": member, "org": actor.organization_id, "u": user},
        )
        for evidence_id in (low, high):
            await connection.execute(
                text(
                    "INSERT INTO "
                    "evidence_originals(id,organization_id,uploader_membership_id,version,"
                    "filename,idempotency_key,storage_key,state,sha256,byte_length,mime_type,"
                    "uploaded_at,expires_at,lease_id,lease_until,confirmed_at) "
                    "VALUES "
                    "(:id,:org,:m,1,'proof.png',:key,:storage,'READY',:sha,100,"
                    "'image/png',now(),now()+interval "
                    "'7 days',:lease,now(),NULL)"
                ),
                {
                    "id": evidence_id,
                    "org": actor.organization_id,
                    "m": actor.membership_id,
                    "key": str(uuid4()),
                    "storage": f"{actor.organization_id}/{evidence_id}/1",
                    "sha": "1" * 64,
                    "lease": uuid4(),
                },
            )
    manager = AuthenticatedActor(
        user,
        f"{user}@example.test",
        "Manager",
        member,
        actor.organization_id,
        "Race",
        MembershipRole.MANAGER,
    )
    blockers = BlockerService(SqlAlchemyBlockerTransactions(sessions))
    refs = tuple(SelectedEvidence(evidence_id=identifier, version=1) for identifier in (low, high))
    baseline = await case.service.create_draft(
        actor, (item(case.tasks[0], evidence_refs=refs),), str(uuid4()), "baseline"
    )
    await case.service.confirm(
        actor,
        ConfirmDailyUpdateCommand(draft_id=baseline.id, expected_draft_version=1),
        str(uuid4()),
        "baseline",
    )
    # Authorize both originals on the Manager's separate Task before the race.
    await blockers.apply(
        actor,
        BlockerCommand(
            task_id=case.tasks[1],
            expected_task_version=1,
            action="CREATE",
            text="Initial proof",
            evidence_refs=refs,
        ),
        str(uuid4()),
    )
    prepared = await case.service.create_draft(
        actor,
        (
            item(
                case.tasks[0],
                expected_progress_version=1,
                evidence_refs=(refs[1],),
                blocker_commands=(
                    BlockerCommand(
                        task_id=case.tasks[0],
                        expected_task_version=1,
                        action="CREATE",
                        text="New blocker",
                        evidence_refs=(refs[0],),
                    ),
                ),
            ),
        ),
        str(uuid4()),
        "race",
    )
    report_first = asyncio.Event()
    manager_first = asyncio.Event()
    original_lock = SqlAlchemyDailyUpdateRepository.lock

    async def coordinated_lock(self: SqlAlchemyDailyUpdateRepository, scope: str) -> None:
        await original_lock(self, scope)
        if not scope.startswith("evidence:"):
            return
        if self.actor.membership_id == actor.membership_id and not report_first.is_set():
            report_first.set()
            if scope == f"evidence:{high}":
                await manager_first.wait()
        if self.actor.membership_id == manager.membership_id and not manager_first.is_set():
            manager_first.set()
            await report_first.wait()

    monkeypatch.setattr(SqlAlchemyDailyUpdateRepository, "lock", coordinated_lock)

    async def report():
        return await case.service.confirm(
            actor,
            ConfirmDailyUpdateCommand(draft_id=prepared.id, expected_draft_version=1),
            str(uuid4()),
            "race",
        )

    async def manager_write():
        await report_first.wait()
        return await blockers.apply(
            manager,
            BlockerCommand(
                task_id=case.tasks[1],
                expected_task_version=1,
                action="CREATE",
                text="Manager proof",
                evidence_refs=refs,
            ),
            str(uuid4()),
        )

    try:
        results = await asyncio.wait_for(asyncio.gather(report(), manager_write()), timeout=10)
        assert len(results) == 2
        assert len(await case.service.history(actor, case.tasks[0])) == 2
    finally:
        await engine.dispose()
