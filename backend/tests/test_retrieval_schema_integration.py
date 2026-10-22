"""Real PostgreSQL owner/tenant RLS and immutable retrieval state."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.retrieval_fixtures import RetrievalHarness, pytestmark, retrieval_harness

__all__ = ["pytestmark", "retrieval_harness"]

TABLES = ("retrieval_source_snapshots", "retrieval_run_budgets", "retrieval_attempt_reservations")


@pytest.mark.asyncio
async def test_retrieval_tables_force_rls_and_runtime_has_no_bypass(
    retrieval_harness: RetrievalHarness,
) -> None:
    h = retrieval_harness
    result = await h.sql(
        "SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname=AN"
        "Y(:tables)",
        {"tables": list(TABLES)},
    )
    assert {row.relname: (row.relrowsecurity, row.relforcerowsecurity) for row in result} == {
        name: (True, True) for name in TABLES
    }
    roles = await h.sql(
        "SELECT rolname,rolbypassrls,rolsuper FROM pg_roles WHERE rolname IN ('app_runtim"
        "e','app_worker')"
    )
    assert list(roles)
    roles = await h.sql(
        "SELECT rolname FROM pg_roles WHERE rolname IN ('app_runtime','app_worker') AND ("
        "rolbypassrls OR rolsuper)"
    )
    assert list(roles) == []


@pytest.mark.parametrize("identity", ["peer", "foreign"])
@pytest.mark.asyncio
async def test_other_identity_cannot_read_or_create_budget(
    retrieval_harness: RetrievalHarness, identity: str
) -> None:
    from app.modules.retrieval.domain.models import RetrievalPolicyError

    h = retrieval_harness
    service = h.service()
    await service.start_run(h.actor, h.run_id, h.lease())
    actor = h.peer if identity == "peer" else h.foreign
    with pytest.raises(RetrievalPolicyError):
        await service.start_run(actor, h.run_id, h.lease())
    async with h.engine.connect() as connection, connection.begin():
        await connection.execute(text("SET LOCAL ROLE app_runtime"))
        await connection.execute(
            text(
                "SELECT set_config('app.organization_id',:org,true),set_config('app.m"
                "embership_id',:member,true)"
            ),
            {"org": str(actor.organization_id), "member": str(actor.membership_id)},
        )
        rows = await connection.execute(text("SELECT * FROM retrieval_run_budgets"))
        assert list(rows) == []
        with pytest.raises(DBAPIError):
            async with connection.begin_nested():
                await connection.execute(
                    text(
                        "INSERT INTO retrieval_run_budgets SELECT * FROM retrieval_ru"
                        "n_budgets WHERE organization_id=:org"
                    ),
                    {"org": h.actor.organization_id},
                )
                # The SELECT is invisible; an explicit spoofed insert must also fail.
                await connection.execute(
                    text(
                        "INSERT INTO "
                        "retrieval_run_budgets(organization_id,agent_run_id,owner_membership_id,orchestration_run_id,job_id,worker_id,job_attempt,fencing_token,scope,caps,reserved,started_at,deadline,expires_at,version)"
                        " SELECT "
                        ":org,:run,:owner,:orch,:job,'x',1,gen_random_uuid(),'{}','{}','{}',now(),now()+interval"
                        " '1 second',now()+interval '1 day',1"
                    ),
                    {
                        "org": h.actor.organization_id,
                        "run": h.run_id,
                        "owner": h.actor.membership_id,
                        "orch": h.orchestration_id,
                        "job": h.job_id,
                    },
                )


@pytest.mark.asyncio
async def test_counters_and_deadline_cannot_be_reset_via_sql(
    retrieval_harness: RetrievalHarness,
) -> None:
    h = retrieval_harness
    service = h.service()
    await service.start_run(h.actor, h.run_id, h.lease())
    await service.reserve_attempt(h.actor, h.run_id, "node", 1, "NODE", lease=h.lease())
    for statement in (
        "UPDATE retrieval_run_budgets SET reserved=jsonb_set(reserved,'{nodes}','0'),"
        "version=version+1 WHERE agent_run_id=:run",
        "UPDATE retrieval_run_budgets SET deadline=deadline+interval '1 second' WHERE age"
        "nt_run_id=:run",
        "DELETE FROM retrieval_run_budgets WHERE agent_run_id=:run",
    ):
        with pytest.raises(DBAPIError):
            await h.sql(statement, {"run": h.run_id})


@pytest.mark.asyncio
async def test_null_counter_cannot_bypass_sql_cap(retrieval_harness: RetrievalHarness) -> None:
    h = retrieval_harness
    await h.service().start_run(h.actor, h.run_id, h.lease())
    with pytest.raises(DBAPIError):
        await h.sql(
            "UPDATE retrieval_run_budgets SET reserved=jsonb_set(reserved,'{nodes}','null'),"
            "version=version+1 WHERE agent_run_id=:run",
            {"run": h.run_id},
        )
