"""Opt-in, synthetic ASGI benchmarks on an explicitly owned local database.

No database reset, hosted provider, Redis mutation, or application activation.
Provision/migrate a separate PostgreSQL instance and set its ownership comment
before using this command. URLs, cookies and instance tokens never enter reports.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import re
import subprocess
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import event, insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_PASSWORD = "RedisBenchmarkOnly-2026!"  # Synthetic fixture only, never a real account.
_WEEK = date(2026, 11, 9)
_CASES = ("workload.person", "workload.team", "workload.mixed", "projects", "tasks", "skills", "me")


class BenchmarkSafetyError(ValueError):
    """Only safe constant codes cross the CLI boundary."""


@dataclass(frozen=True)
class BenchmarkTarget:
    database_url: SecretStr
    instance_token: SecretStr
    database_name: str
    environment_fingerprint: str


class FixtureSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str = "redis-benchmark.v1"
    tenants: int = Field(default=2, ge=2, le=2)
    members_per_tenant: int = Field(default=100, ge=2, le=100)
    projects_per_tenant: int = Field(default=50, ge=1, le=50)
    tasks_per_tenant: int = Field(default=5_000, ge=12, le=5_000)
    weeks_per_project: int = Field(default=12, ge=1, le=12)

    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


def validate_target(database_url: str, instance_token: str) -> BenchmarkTarget:
    try:
        url = make_url(database_url)
    except (ArgumentError, ValueError):
        raise BenchmarkSafetyError("DEDICATED_DATABASE_REQUIRED") from None
    try:
        valid = (
            url.drivername == "postgresql+psycopg"
            and url.host in {"127.0.0.1", "localhost", "::1"}
            and url.port is not None
            and url.port != 5432
            and not url.query
            and re.fullmatch(r"[a-f0-9]{32}", instance_token) is not None
            and url.database == f"wm_redis_bench_{instance_token[:12]}"
        )
    except ValueError:
        valid = False
    if not valid:
        raise BenchmarkSafetyError("DEDICATED_DATABASE_REQUIRED")
    identity = f"{url.host}:{url.port}/{url.database}:{instance_token}"
    return BenchmarkTarget(
        SecretStr(database_url),
        SecretStr(instance_token),
        str(url.database),
        hashlib.sha256(identity.encode()).hexdigest(),
    )


def require_fixture_setup(allowed: bool) -> None:
    if not allowed:
        raise BenchmarkSafetyError("FIXTURE_SETUP_NOT_AUTHORIZED")


@dataclass(frozen=True)
class Sample:
    latency_ms: float
    sql_statements: int
    sql_ms: float
    status: int
    operation: str = "read"
    response_bytes: int = 0


def summarize(samples: Sequence[Sample]) -> dict[str, Any]:
    if not samples:
        raise BenchmarkSafetyError("NO_MEASUREMENTS")
    ordered = sorted(s.latency_ms for s in samples)
    count = len(samples)
    return {
        "samples": count,
        "errors": sum(not 200 <= s.status < 300 for s in samples),
        **{f"p{q}_ms": ordered[math.ceil(count * q / 100) - 1] for q in (50, 95, 99)},
        "sql_statements_per_request": sum(s.sql_statements for s in samples) / count,
        "sql_ms_per_request": sum(s.sql_ms for s in samples) / count,
        "response_bytes_max": max(s.response_bytes for s in samples),
    }


def build_report(
    *,
    target: BenchmarkTarget,
    fixture: FixtureSpec,
    revision: str,
    series: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": "redis-benchmark.v1",
        "mode": "baseline",
        "recorded_at": datetime.now(UTC).isoformat(),
        "revision": revision,
        "environment_fingerprint": target.environment_fingerprint,
        "dataset_fingerprint": fixture.fingerprint(),
        "fixture": fixture.model_dump(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "provider": "disabled",
        "redis_measurements": "NOT_ENABLED",
        "series": series,
    }


def validate_comparison(baseline: dict[str, Any], current: dict[str, Any]) -> None:
    for key in ("schema_version", "environment_fingerprint", "dataset_fingerprint", "fixture"):
        if baseline.get(key) != current.get(key):
            raise BenchmarkSafetyError("DATASET_MISMATCH")
    for key in ("python", "cpu_count"):
        if baseline.get(key) != current.get(key):
            raise BenchmarkSafetyError("ENVIRONMENT_MISMATCH")


def fixture_id(target: BenchmarkTarget, kind: str, tenant: int, index: int = 0) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"redis-bench:{target.instance_token.get_secret_value()}:{kind}:{tenant}:{index}",
    )


async def assert_owned_database(engine: AsyncEngine, target: BenchmarkTarget) -> str:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT current_database(), shobj_description(oid, 'pg_database') "
                    "FROM pg_database WHERE datname=current_database()"
                )
            )
        ).one()
    expected = f"workcrew-redis-benchmark:{target.instance_token.get_secret_value()}"
    marker = str(row[1] or "")
    if row[0] != target.database_name or marker.split("|")[0] != expected:
        raise BenchmarkSafetyError("DATABASE_OWNERSHIP_NOT_VERIFIED")
    return marker


async def setup_fixture(
    engine: AsyncEngine,
    target: BenchmarkTarget,
    fixture: FixtureSpec,
    *,
    allowed: bool,
) -> None:
    require_fixture_setup(allowed)
    marker = await assert_owned_database(engine, target)
    from pwdlib import PasswordHash

    from app.modules.identity.adapters.database_models import UserModel
    from app.modules.organization.adapters.database_models import MembershipModel, OrganizationModel
    from app.modules.people_capacity.adapters.database_models import (
        CapacityEntryModel,
        LeaveEntryModel,
    )
    from app.modules.work.adapters.database_models import ProjectModel, TaskModel
    from app.modules.work.planning.adapters.database_models import ProjectWeekModel

    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(20261112, 1)"))
        existing = await conn.scalar(text("SELECT count(*) FROM organizations"))
        if existing:
            if marker.endswith("|" + fixture.fingerprint()):
                return
            raise BenchmarkSafetyError("FIXTURE_ALREADY_EXISTS_WITH_DIFFERENT_IDENTITY")
        password_hash = PasswordHash.recommended().hash(_PASSWORD)
        for tenant in range(fixture.tenants):
            org = fixture_id(target, "org", tenant)
            member_ids = [
                fixture_id(target, "member", tenant, i) for i in range(fixture.members_per_tenant)
            ]
            await conn.execute(
                insert(OrganizationModel),
                {
                    "id": org,
                    "slug": f"redis-bench-{tenant}",
                    "name": "Synthetic benchmark",
                },
            )
            await conn.execute(
                insert(UserModel),
                [
                    {
                        "id": fixture_id(target, "user", tenant, i),
                        "email_normalized": f"bench-{tenant}-{i}@example.test",
                        "email_display": f"bench-{tenant}-{i}@example.test",
                        "display_name": f"Synthetic member {i}",
                        "password_hash": password_hash,
                    }
                    for i in range(fixture.members_per_tenant)
                ],
            )
            await conn.execute(
                insert(MembershipModel),
                [
                    {
                        "id": mid,
                        "organization_id": org,
                        "user_id": fixture_id(target, "user", tenant, i),
                        "role": "MANAGER" if i == 0 else "EMPLOYEE",
                    }
                    for i, mid in enumerate(member_ids)
                ],
            )
            await conn.execute(
                insert(ProjectModel),
                [
                    {
                        "id": fixture_id(target, "project", tenant, i),
                        "organization_id": org,
                        "name": f"Synthetic project {i}",
                        "created_by_membership_id": member_ids[0],
                        "updated_by_membership_id": member_ids[0],
                    }
                    for i in range(fixture.projects_per_tenant)
                ],
            )
            await conn.execute(
                insert(ProjectWeekModel),
                [
                    {
                        "id": fixture_id(target, "week", tenant, p * fixture.weeks_per_project + w),
                        "organization_id": org,
                        "project_id": fixture_id(target, "project", tenant, p),
                        "week_number": w + 1,
                        "start_date": _WEEK + timedelta(weeks=w),
                        "end_date": _WEEK + timedelta(weeks=w, days=6),
                        "objective": "Synthetic week",
                        "status": "PLANNED",
                        "created_by_membership_id": member_ids[0],
                        "updated_by_membership_id": member_ids[0],
                    }
                    for p in range(fixture.projects_per_tenant)
                    for w in range(fixture.weeks_per_project)
                ],
            )
            # Batch parameters to stay below PostgreSQL/driver bind limits.
            for offset in range(0, fixture.tasks_per_tenant, 500):
                await conn.execute(
                    insert(TaskModel),
                    [
                        {
                            "id": fixture_id(target, "task", tenant, i),
                            "organization_id": org,
                            "project_id": fixture_id(
                                target, "project", tenant, i % fixture.projects_per_tenant
                            ),
                            "project_week_id": fixture_id(
                                target,
                                "week",
                                tenant,
                                (i % fixture.projects_per_tenant) * fixture.weeks_per_project
                                + (i // fixture.projects_per_tenant) % fixture.weeks_per_project,
                            ),
                            "title": f"Synthetic task {i}",
                            "assignee_membership_id": member_ids[i % fixture.members_per_tenant],
                            "estimated_effort_hours": 4,
                            "required_skill_labels": [],
                            "status": "TO_DO",
                            "created_by_membership_id": member_ids[0],
                            "updated_by_membership_id": member_ids[0],
                        }
                        for i in range(offset, min(offset + 500, fixture.tasks_per_tenant))
                    ],
                )
            await conn.execute(
                insert(CapacityEntryModel),
                [
                    {
                        "id": fixture_id(target, "capacity", tenant, i),
                        "organization_id": org,
                        "membership_id": mid,
                        "kind": "DEFAULT",
                        "hours": 40,
                        "effective_from": _WEEK,
                        "effective_to": _WEEK + timedelta(weeks=52),
                    }
                    for i, mid in enumerate(member_ids)
                ],
            )
            await conn.execute(
                insert(LeaveEntryModel),
                [
                    {
                        "id": fixture_id(target, "leave", tenant, i),
                        "organization_id": org,
                        "membership_id": member_ids[i],
                        "start_date": _WEEK,
                        "end_date": _WEEK,
                        "unavailable_hours": 8,
                    }
                    for i in range(0, fixture.members_per_tenant, 10)
                ],
            )
        # Both strings are validated local identifiers, not arbitrary SQL input.
        comment = marker.split("|")[0] + "|" + fixture.fingerprint()
        await conn.execute(text(f"COMMENT ON DATABASE {target.database_name} IS '{comment}'"))


async def assert_fixture(
    engine: AsyncEngine,
    target: BenchmarkTarget,
    fixture: FixtureSpec,
) -> dict[str, int]:
    marker = await assert_owned_database(engine, target)
    if not marker.endswith("|" + fixture.fingerprint()):
        raise BenchmarkSafetyError("VERIFIED_FIXTURE_REQUIRED")
    expected = {
        "organizations": fixture.tenants,
        "memberships": fixture.tenants * fixture.members_per_tenant,
        "projects": fixture.tenants * fixture.projects_per_tenant,
        "project_weeks": fixture.tenants * fixture.projects_per_tenant * fixture.weeks_per_project,
        "tasks": fixture.tenants * fixture.tasks_per_tenant,
        "capacity_entries": fixture.tenants * fixture.members_per_tenant,
    }
    actual: dict[str, int] = {}
    async with engine.connect() as connection:
        for table, count in expected.items():
            actual[table] = int(await connection.scalar(text(f"SELECT count(*) FROM {table}")) or 0)
            if actual[table] != count:
                raise BenchmarkSafetyError("FIXTURE_INPUTS_CHANGED")
        changed = await connection.scalar(
            text(
                "SELECT (SELECT count(*) FROM capacity_entries WHERE hours<>40) + "
                "(SELECT count(*) FROM tasks WHERE estimated_effort_hours IS DISTINCT FROM 4 "
                "OR status<>'TO_DO') + (SELECT count(*) FROM memberships WHERE NOT is_active)"
            )
        )
        if changed:
            raise BenchmarkSafetyError("FIXTURE_INPUTS_CHANGED")
    return actual


@dataclass
class SQLCost:
    statements: int = 0
    seconds: float = 0
    starts: list[float] = field(default_factory=lambda: list[float]())


_COST: ContextVar[SQLCost | None] = ContextVar("redis_benchmark_sql_cost", default=None)


def instrument_sql(engine: AsyncEngine) -> None:
    def before(*args: Any) -> None:
        cost = _COST.get()
        if cost is not None:
            cost.statements += 1
            cost.starts.append(perf_counter())

    def after(*args: Any) -> None:
        cost = _COST.get()
        if cost is not None and cost.starts:
            cost.seconds += perf_counter() - cost.starts.pop()

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    event.listen(engine.sync_engine, "after_cursor_execute", after)


async def measure_request(
    client: AsyncClient,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Sample:
    cost = SQLCost()
    token = _COST.set(cost)
    start = perf_counter()
    try:
        response = await client.request(
            "GET" if body is None else "POST", path, json=body, headers=headers
        )
        return Sample(
            (perf_counter() - start) * 1000,
            cost.statements,
            cost.seconds * 1000,
            response.status_code,
            "read" if body is None else "write",
            len(response.content),
        )
    finally:
        _COST.reset(token)


def read_path(case: str, target: BenchmarkTarget, tenant: int) -> str:
    if case in {"workload.person", "workload.mixed"}:
        return (
            f"/api/v1/workload?week_start={_WEEK}&membership_id="
            f"{fixture_id(target, 'member', tenant, 1)}"
        )
    return {
        "workload.team": f"/api/v1/workload?week_start={_WEEK}",
        "projects": "/api/v1/projects",
        "tasks": "/api/v1/tasks",
        "skills": "/api/v1/skills",
        "me": "/api/v1/me",
    }[case]


async def run_series(
    clients: list[AsyncClient],
    target: BenchmarkTarget,
    *,
    case: str,
    concurrency: int,
    duration_seconds: float,
) -> dict[str, Any]:
    samples: list[Sample] = []
    write_states: list[dict[str, Any]] = []
    first_requests: list[Sample] = []
    for tenant, client in enumerate(clients):
        warmup = await measure_request(client, read_path(case, target, tenant))
        first_requests.append(warmup)
        if warmup.status != 200:
            raise BenchmarkSafetyError("WARMUP_FAILED")
    if case == "workload.mixed":
        for index in range(concurrency):
            tenant = index % len(clients)
            member = fixture_id(target, "member", tenant, index // len(clients) + 1)
            capacity = await clients[tenant].get(f"/api/v1/capacity?membership_id={member}")
            if capacity.status_code != 200 or len(capacity.json()) != 1:
                raise BenchmarkSafetyError("CAPACITY_FIXTURE_REQUIRED")
            value = capacity.json()[0]
            if value["hours"] != 40:
                raise BenchmarkSafetyError("FIXTURE_INPUTS_CHANGED")
            write_states.append({"member": str(member), "version": value["version"], "hours": 40})
    deadline = perf_counter() + duration_seconds

    async def worker(index: int) -> None:
        tenant = index % len(clients)
        number = 0
        while perf_counter() < deadline:
            number += 1
            if case == "workload.mixed" and number % 10 == 0:
                state = write_states[index]
                hours = 39 if state["hours"] == 40 else 40
                sample = await measure_request(
                    clients[tenant],
                    "/api/v1/capacity",
                    body={
                        "membership_id": state["member"],
                        "kind": "DEFAULT",
                        "hours": hours,
                        "effective_from": str(_WEEK),
                        "effective_to": str(_WEEK + timedelta(weeks=52)),
                    },
                    headers={"Idempotency-Key": uuid4().hex, "If-Match": f'"{state["version"]}"'},
                )
                if 200 <= sample.status < 300:
                    state["version"] += 1
                    state["hours"] = hours
            else:
                sample = await measure_request(clients[tenant], read_path(case, target, tenant))
            samples.append(sample)
            if len(samples) >= 100_000:
                raise BenchmarkSafetyError("MEASUREMENT_CAP_EXCEEDED")

    try:
        await asyncio.gather(*(worker(i) for i in range(concurrency)))
    finally:
        # Compensate synthetic mutations through the same authorized application path.
        for index, state in enumerate(write_states):
            if state["hours"] == 40:
                continue
            restored = await measure_request(
                clients[index % len(clients)],
                "/api/v1/capacity",
                body={
                    "membership_id": state["member"],
                    "kind": "DEFAULT",
                    "hours": 40,
                    "effective_from": str(_WEEK),
                    "effective_to": str(_WEEK + timedelta(weeks=52)),
                },
                headers={"Idempotency-Key": uuid4().hex, "If-Match": f'"{state["version"]}"'},
            )
            if restored.status != 200:
                raise BenchmarkSafetyError("FIXTURE_COMPENSATION_FAILED")
    reads = [s for s in samples if s.operation == "read"]
    writes = [s for s in samples if s.operation == "write"]
    return {
        "case": case,
        "concurrency": concurrency,
        "duration_seconds": duration_seconds,
        "first_requests": summarize(first_requests),
        **summarize(samples),
        "reads": summarize(reads) if reads else None,
        "writes": summarize(writes) if writes else None,
    }


async def run_benchmark(
    target: BenchmarkTarget,
    fixture: FixtureSpec,
    *,
    cases: Sequence[str],
    concurrencies: Sequence[int],
    duration_seconds: float,
    repetitions: int,
) -> list[dict[str, Any]]:
    from contextlib import AsyncExitStack

    from app.core.config import Settings
    from app.main import create_app

    async with AsyncExitStack() as stack:
        clients: list[AsyncClient] = []
        for tenant in range(fixture.tenants):
            app = create_app(
                Settings(
                    environment="test",
                    database_url=target.database_url.get_secret_value(),
                    local_auth_organization_slug=f"redis-bench-{tenant}",
                    ai_provider="disabled",
                    langsmith_tracing=False,
                )
            )
            await stack.enter_async_context(app.router.lifespan_context(app))
            instrument_sql(app.state.database_engine)
            client = await stack.enter_async_context(
                AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://benchmark.test",
                )
            )
            login = await client.post(
                "/api/v1/auth/login",
                json={
                    "email": f"bench-{tenant}-0@example.test",
                    "password": _PASSWORD,
                },
            )
            if login.status_code != 200:
                raise BenchmarkSafetyError("FIXTURE_LOGIN_FAILED")
            clients.append(client)
        results: list[dict[str, Any]] = []
        for case in cases:
            for concurrency in concurrencies:
                for repeat in range(repetitions):
                    result = await run_series(
                        clients,
                        target,
                        case=case,
                        concurrency=concurrency,
                        duration_seconds=duration_seconds,
                    )
                    results.append({"repeat": repeat + 1, **result})
        return results


async def _run(args: argparse.Namespace) -> int:
    target = validate_target(
        os.environ.get("APP_DATABASE_URL", ""), os.environ.get("REDIS_BENCHMARK_INSTANCE_TOKEN", "")
    )
    fixture = FixtureSpec()
    baseline: dict[str, Any] | None = None
    if args.mode == "compare":
        if args.baseline is None:
            raise BenchmarkSafetyError("BASELINE_REQUIRED")
        try:
            decoded = json.loads(args.baseline.read_text())
        except json.JSONDecodeError:
            raise BenchmarkSafetyError("INVALID_BASELINE") from None
        if not isinstance(decoded, dict):
            raise BenchmarkSafetyError("INVALID_BASELINE")
        baseline = cast(dict[str, Any], decoded)
        validate_comparison(
            baseline, build_report(target=target, fixture=fixture, revision="", series=[])
        )
    engine = create_async_engine(target.database_url.get_secret_value())
    try:
        marker = await assert_owned_database(engine, target)
        if args.allow_fixture_setup:
            await setup_fixture(engine, target, fixture, allowed=True)
        elif not marker.endswith("|" + fixture.fingerprint()):
            raise BenchmarkSafetyError("VERIFIED_FIXTURE_REQUIRED")
        cardinalities = await assert_fixture(engine, target, fixture)
        series = await run_benchmark(
            target,
            fixture,
            cases=args.case,
            concurrencies=args.concurrency,
            duration_seconds=args.duration,
            repetitions=args.repetitions,
        )
    finally:
        await engine.dispose()
    revision = (
        await asyncio.to_thread(subprocess.check_output, ["git", "rev-parse", "HEAD"], text=True)
    ).strip()
    report = build_report(target=target, fixture=fixture, revision=revision, series=series)
    report["actual_cardinalities"] = cardinalities
    if baseline is not None:
        validate_comparison(baseline, report)
        report["mode"] = "compare"
        report["baseline_revision"] = baseline["revision"]
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    return int(any(s["errors"] for s in series))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("baseline", "compare"), default="baseline")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--allow-fixture-setup", action="store_true")
    parser.add_argument("--case", choices=_CASES, nargs="+", default=["workload.person"])
    parser.add_argument("--concurrency", type=int, nargs="+", default=[10, 50])
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args(argv)
    if (
        not 0 < args.duration <= 300
        or not 1 <= args.repetitions <= 5
        or any(not 1 <= n <= 50 for n in args.concurrency)
    ):
        parser.error("invalid measurement bounds")
    try:
        return asyncio.run(_run(args))
    except BenchmarkSafetyError as exc:
        print(str(exc))
        return 2
    except (SQLAlchemyError, OSError):
        print("BENCHMARK_DATABASE_OR_OUTPUT_UNAVAILABLE")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
