"""Risk context is re-resolved under current membership, never cached authority."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.progress.domain.blockers import BlockerError
from app.modules.risk.adapters.repository import RiskTransactions
from app.modules.risk.application.read_service import RiskReadService
from tests.test_daily_update_api_integration import seed_task
from tests.test_evidence_api_integration import Harness, harness
from tests.test_risk_api_integration import pytestmark, setup, severe_blocker

__all__ = ["harness", "pytestmark"]


def reader(h: Harness) -> RiskReadService:
    return RiskReadService(
        RiskTransactions(
            async_sessionmaker(
                bind=h.connection,
                class_=AsyncSession,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
        )
    )


@pytest.mark.asyncio
async def test_employee_own_context_has_no_manager_assessment_or_other_person_capacity(
    harness: Harness,
):
    task = UUID(await seed_task(harness))
    _, risks, _, manager = await setup(harness)
    await severe_blocker(harness, manager, task)
    await risks.request_refresh(manager, task, str(uuid4()))
    assert await risks.run_once(manager)
    service = reader(harness)
    full = await service.read(manager, task)
    own = await service.read(harness.actor, task)
    assert full.score == "83"
    assert own.score is None and own.scope == "OWN_WORK"
    assert own.risk_assessment_id is None
    assert all(f.kind in {"TASK", "PROGRESS", "BLOCKER", "WARNING"} for f in own.permitted_sources)
    assert full.rationale not in own.rationale
    with pytest.raises(BlockerError):
        await service.read(harness.foreign, task)


@pytest.mark.asyncio
async def test_context_revalidation_rejects_reassignment_role_change_and_source_edit(
    harness: Harness,
):
    task = UUID(await seed_task(harness))
    _, _, _, manager = await setup(harness)
    service = reader(harness)
    before = await service.read(manager, task)
    await harness.sql("UPDATE tasks SET version=version+1 WHERE id=:id", {"id": task})
    with pytest.raises(BlockerError):
        await service.read(manager, task, before.fingerprint)
    await harness.sql(
        "UPDATE memberships SET role='EMPLOYEE' WHERE id=:id", {"id": manager.membership_id}
    )
    with pytest.raises(BlockerError):
        await service.read(manager, task)


@pytest.mark.asyncio
async def test_card_cannot_replace_authoritative_snapshot_score(harness: Harness):
    from app.modules.assistant.adapters.risk_tools import RiskBlockProjector

    task = UUID(await seed_task(harness))
    _, risks, _, manager = await setup(harness)
    await risks.request_refresh(manager, task, str(uuid4()))
    assert await risks.run_once(manager)
    service = reader(harness)
    current = await service.read(manager, task)
    content = current.model_dump(mode="json")
    content["score"] = "0"
    card = dict(kind="risk", task_id=str(task), fingerprint=current.fingerprint, content=content)
    result = await RiskBlockProjector(service).project(manager, card)
    assert result["kind"] == "safe_error"


@pytest.mark.asyncio
async def test_own_factual_observations_can_be_explained_without_manager_score(harness: Harness):
    task = UUID(await seed_task(harness))
    own = await reader(harness).read(harness.actor, task)
    assert own.observations
    assert all(
        set(o.source_ids).issubset({f.id for f in own.permitted_sources}) for o in own.observations
    )
    assert own.score is None
