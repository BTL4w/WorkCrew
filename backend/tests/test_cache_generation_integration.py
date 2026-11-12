"""Generation triggers must commit/rollback with every workload source."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.scripts.benchmark_redis import fixture_id, validate_target

pytestmark = [
    pytest.mark.integration,
    pytest.mark.redis_integration,
    pytest.mark.skipif(
        os.getenv("RUN_REDIS_INTEGRATION") != "1",
        reason="requires owned cache test database",
    ),
]


@pytest.mark.asyncio
async def test_generation_table_is_tenant_owned_and_force_rls() -> None:
    target = validate_target(
        os.environ["APP_DATABASE_URL"], os.environ["REDIS_BENCHMARK_INSTANCE_TOKEN"]
    )
    engine = create_async_engine(target.database_url.get_secret_value())
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity "
                        "FROM pg_class WHERE oid=to_regclass('read_model_generations')"
                    )
                )
            ).one_or_none()
            assert row is not None, "generation table is missing"
            assert tuple(row) == (True, True)
            role = (
                await conn.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname='app_runtime'")
                )
            ).one()
            assert tuple(role) == (False, False)
            await conn.execute(text("SET LOCAL ROLE app_runtime"))
            assert (await conn.execute(text("SELECT * FROM read_model_generations"))).all() == []
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "table,assignment,kind",
    [
        ("capacity_entries", "hours=hours+1", "capacity"),
        ("leave_entries", "unavailable_hours=unavailable_hours+1", "leave"),
        ("tasks", "estimated_effort_hours=estimated_effort_hours+1", "task"),
        ("project_weeks", "objective=objective || ' synthetic change'", "week"),
        ("memberships", "is_active=NOT is_active", "member"),
    ],
)
@pytest.mark.asyncio
async def test_each_workload_source_bumps_revision_and_rollback_preserves_it(
    table: str,
    assignment: str,
    kind: str,
) -> None:
    target = validate_target(
        os.environ["APP_DATABASE_URL"], os.environ["REDIS_BENCHMARK_INSTANCE_TOKEN"]
    )
    engine = create_async_engine(target.database_url.get_secret_value())
    org = fixture_id(target, "org", 0)
    resource = fixture_id(target, kind, 0, 0)
    try:
        async with engine.connect() as conn:
            initial = await conn.scalar(
                text("SELECT generation FROM read_model_generations WHERE organization_id=:org"),
                {"org": org},
            )
            await conn.rollback()
            transaction = await conn.begin()
            await conn.execute(
                text(f"UPDATE {table} SET {assignment} WHERE id=:id"), {"id": resource}
            )
            changed = await conn.scalar(
                text("SELECT generation FROM read_model_generations WHERE organization_id=:org"),
                {"org": org},
            )
            assert changed > initial
            await transaction.rollback()
            after = await conn.scalar(
                text("SELECT generation FROM read_model_generations WHERE organization_id=:org"),
                {"org": org},
            )
            assert after == initial
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_cannot_read_or_modify_another_tenant_generation() -> None:
    target = validate_target(
        os.environ["APP_DATABASE_URL"], os.environ["REDIS_BENCHMARK_INSTANCE_TOKEN"]
    )
    engine = create_async_engine(target.database_url.get_secret_value())
    org, other = fixture_id(target, "org", 0), fixture_id(target, "org", 1)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    "SELECT set_config('role','app_runtime',true), "
                    "set_config('app.organization_id',:org,true)"
                ),
                {"org": str(org)},
            )
            visible = (
                (await conn.execute(text("SELECT organization_id FROM read_model_generations")))
                .scalars()
                .all()
            )
            assert visible == [org]
            modified = await conn.execute(
                text(
                    "UPDATE read_model_generations "
                    "SET generation=generation+1 WHERE organization_id=:org"
                ),
                {"org": other},
            )
            assert modified.rowcount == 0
            savepoint = await conn.begin_nested()
            from sqlalchemy.exc import DBAPIError

            with pytest.raises(DBAPIError):
                await conn.execute(
                    text(
                        "INSERT INTO read_model_generations "
                        "(organization_id,epoch,generation) VALUES (:org,:epoch,0)"
                    ),
                    {"org": other, "epoch": uuid4()},
                )
            await savepoint.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_insert_delete_and_bulk_writes_invalidate_inside_the_source_transaction() -> None:
    target = validate_target(
        os.environ["APP_DATABASE_URL"], os.environ["REDIS_BENCHMARK_INSTANCE_TOKEN"]
    )
    engine = create_async_engine(target.database_url.get_secret_value())
    org, member = fixture_id(target, "org", 0), fixture_id(target, "member", 0, 1)
    try:
        async with engine.connect() as conn, conn.begin():
            before = await conn.scalar(
                text("SELECT generation FROM read_model_generations WHERE organization_id=:org"),
                {"org": org},
            )
            resource = uuid4()
            await conn.execute(
                text(
                    "INSERT INTO capacity_entries (id,organization_id,"
                    "membership_id,kind,hours,effective_from,effective_to,week_start) VALUES "
                    "(:id,:org,:member,'OVERRIDE',35,'2027-02-08','2027-02-14','2027-02-08')"
                ),
                {"id": resource, "org": org, "member": member},
            )
            inserted = await conn.scalar(
                text("SELECT generation FROM read_model_generations WHERE organization_id=:org"),
                {"org": org},
            )
            assert inserted > before
            await conn.execute(text("DELETE FROM capacity_entries WHERE id=:id"), {"id": resource})
            deleted = await conn.scalar(
                text("SELECT generation FROM read_model_generations WHERE organization_id=:org"),
                {"org": org},
            )
            assert deleted > inserted
            savepoint = await conn.begin_nested()
            await conn.execute(
                text("UPDATE tasks SET estimated_effort_hours=5 WHERE organization_id=:org"),
                {"org": org},
            )
            bulk = await conn.scalar(
                text("SELECT generation FROM read_model_generations WHERE organization_id=:org"),
                {"org": org},
            )
            assert bulk > deleted
            await savepoint.rollback()
            assert (
                await conn.scalar(
                    text(
                        "SELECT generation FROM read_model_generations WHERE organization_id=:org"
                    ),
                    {"org": org},
                )
                == deleted
            )
            # No fixture mutation is committed by this coverage test.
            await conn.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_writer_cannot_change_the_readers_source_snapshot() -> None:
    from datetime import date

    from app.core.database import create_session_factory
    from app.modules.identity.domain.auth import AuthenticatedActor
    from app.modules.organization.domain.roles import MembershipRole
    from app.modules.people_capacity.adapters.workload_cache import (
        SqlAlchemyWorkloadReadTransactions,
    )

    target = validate_target(
        os.environ["APP_DATABASE_URL"], os.environ["REDIS_BENCHMARK_INSTANCE_TOKEN"]
    )
    engine = create_async_engine(target.database_url.get_secret_value())
    owner = AuthenticatedActor(
        fixture_id(target, "user", 0, 0),
        "synthetic@example.test",
        "Synthetic",
        fixture_id(target, "member", 0, 0),
        fixture_id(target, "org", 0),
        "Synthetic",
        MembershipRole.MANAGER,
    )
    member, capacity = fixture_id(target, "member", 0, 1), fixture_id(target, "capacity", 0, 1)
    snapshots = SqlAlchemyWorkloadReadTransactions(create_session_factory(engine))
    try:
        async with snapshots(owner) as snapshot:
            old_revision = snapshot.revision
            initial = await snapshot.load(week_start=date(2026, 11, 9), membership_id=member)
            assert initial[0].effective_capacity_hours == 40
            async with engine.begin() as writer:
                await writer.execute(
                    text("UPDATE capacity_entries SET hours=39 WHERE id=:id"), {"id": capacity}
                )
            repeat = await snapshot.load(week_start=date(2026, 11, 9), membership_id=member)
            assert repeat == initial
            assert snapshot.revision == old_revision
        async with snapshots(owner) as current:
            assert current.revision != old_revision
            updated = await current.load(week_start=date(2026, 11, 9), membership_id=member)
            assert updated[0].effective_capacity_hours == 39
    finally:
        async with engine.begin() as writer:
            await writer.execute(
                text("UPDATE capacity_entries SET hours=40 WHERE id=:id"), {"id": capacity}
            )
        await engine.dispose()
