"""Tenant-scoped, non-BYPASSRLS cleanup over a closed storage allowlist."""

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import exists, func, select, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql.elements import ColumnElement

from app.core.config import get_settings

from ..domain.retention import PAYLOAD_FIELDS, PayloadClass, PayloadLocator, PurgeRecord
from .payload_models import RetentionPayloadModel


def live_payload(model: type[DeclarativeBase], kind: str) -> ColumnElement[bool]:
    source = model.__table__.c
    registry = RetentionPayloadModel
    return ~exists(
        select(1).where(
            registry.organization_id == source.organization_id,
            registry.storage_kind == kind,
            registry.owner_id == source.id,
            (
                func.least(
                    registry.expires_at,
                    registry.created_at
                    + timedelta(days=get_settings().ai_raw_context_retention_days),
                )
                <= func.clock_timestamp()
            )
            | registry.purged_at.is_not(None),
        )
    )


async def context_expired(
    session: AsyncSession, organization_id: UUID, kind: str, owner_id: UUID
) -> bool:
    expired = await session.scalar(
        text(
            "SELECT EXISTS(SELECT 1 FROM ai_retention_payloads WHERE "
            "organization_id=:org AND storage_kind=:kind AND owner_id=:owner AND "
            "(LEAST(expires_at,created_at+make_interval(days=>:days))<=clock_timestamp()"
            " OR purged_at IS NOT NULL))"
        ),
        {
            "org": organization_id,
            "kind": kind,
            "owner": owner_id,
            "days": get_settings().ai_raw_context_retention_days,
        },
    )
    return bool(expired)


async def assert_context_live(
    session: AsyncSession, organization_id: UUID, kind: str, owner_id: UUID
) -> None:
    if await context_expired(session, organization_id, kind, owner_id):
        raise ValueError("CONTEXT_EXPIRED")


class SQLRetentionRepository:
    def __init__(self, session: AsyncSession, organization_id: UUID):
        self.session, self.organization_id = session, organization_id
        self.now: datetime | None = None

    async def claim_expired(
        self, organization_id: UUID, now: datetime, limit: int
    ) -> tuple[PayloadLocator, ...]:
        if now.tzinfo is None or not 1 <= limit <= 100:
            raise ValueError("INVALID_RETENTION_BATCH")
        if organization_id != self.organization_id:
            raise ValueError("RETENTION_TENANT_MISMATCH")
        self.now = now
        await self.session.execute(
            text("SELECT set_config('app.retention_now',:now,true)"), {"now": now.isoformat()}
        )
        rows = (
            (
                await self.session.execute(
                    text(
                        (
                            "SELECT * FROM ai_retention_payloads WHERE organization_id=:org AND "
                            "purged_at IS NULL AND "
                            "LEAST(expires_at,created_at+make_interval(days=>:days))<=:now "
                            "ORDER BY "
                            "expires_at,storage_kind,owner_id LIMIT :limit FOR UPDATE SKIP LOCKED"
                        )
                    ),
                    {
                        "org": organization_id,
                        "now": now,
                        "limit": limit,
                        "days": get_settings().ai_raw_context_retention_days,
                    },
                )
            )
            .mappings()
            .all()
        )
        available: list[RowMapping] = []
        for row in rows:
            kind = row["storage_kind"]
            if kind not in PAYLOAD_FIELDS:
                raise ValueError("UNKNOWN_PAYLOAD_STORAGE")
            locked = await self.session.scalar(
                text(
                    f"SELECT id FROM {kind} WHERE organization_id=:org AND id=:id "
                    "FOR UPDATE SKIP LOCKED"
                ),
                {"org": organization_id, "id": row["owner_id"]},
            )
            if locked is not None:
                available.append(row)
        return tuple(
            PayloadLocator(
                organization_id=organization_id,
                storage_kind=r["storage_kind"],
                owner_id=r["owner_id"],
                classification=PayloadClass(r["classification"]),
                created_at=r["created_at"],
                expires_at=r["expires_at"],
                run_id=r["run_id"],
                fence=r["fence"],
            )
            for r in available
        )

    async def purge(self, locator: PayloadLocator, fence: int) -> PurgeRecord:
        if (
            locator.organization_id != self.organization_id
            or fence != locator.fence
            or self.now is None
        ):
            raise ValueError("RETENTION_FENCE_LOST")
        fields = PAYLOAD_FIELDS[locator.storage_kind]
        assignments: list[str] = []
        values: dict[str, object] = {"org": self.organization_id, "owner": locator.owner_id}
        for field, replacement in fields.items():
            if isinstance(replacement, (dict, list)):
                assignments.append(f"{field}=CAST(:{field} AS jsonb)")
                values[field] = json.dumps(replacement)
            else:
                assignments.append(f"{field}=:{field}")
                values[field] = replacement
        # Table and column names are selected exclusively by the static domain allowlist.
        await self.session.execute(
            text(
                f"UPDATE {locator.storage_kind} SET {','.join(assignments)} "
                "WHERE organization_id=:org AND id=:owner"
            ),
            values,
        )
        await self._invalidate(locator)
        changed = await self.session.execute(
            text(
                "UPDATE ai_retention_payloads SET purged_at=:now,fence=fence+1 WHERE "
                "organization_id=:org AND storage_kind=:kind AND owner_id=:owner AND "
                "fence=:fence AND purged_at IS NULL AND "
                "LEAST(expires_at,created_at+make_interval(days=>:days))<=:now"
            ),
            {
                "org": self.organization_id,
                "kind": locator.storage_kind,
                "owner": locator.owner_id,
                "fence": fence,
                "now": self.now,
                "days": get_settings().ai_raw_context_retention_days,
            },
        )
        if changed.rowcount != 1:  # type: ignore[attr-defined]
            raise ValueError("RETENTION_FENCE_LOST")
        return PurgeRecord(locator.owner_id, fence + 1)

    async def _invalidate(self, locator: PayloadLocator) -> None:
        # Business approvals awaiting a decision keep their identity/node and manual path.
        if locator.storage_kind == "orchestration_runs":
            await self.session.execute(
                text(
                    "UPDATE orchestration_runs SET "
                    "status='FAILED',safe_error_code='CONTEXT_EXPIRED',stop_reason='CONTEXT_EXPIRED'"
                    " WHERE organization_id=:org AND id=:id AND status NOT IN "
                    "('COMPLETED','FAILED')"
                ),
                {"org": self.organization_id, "id": locator.owner_id},
            )
            await self.session.execute(
                text(
                    "UPDATE report_generation_jobs SET "
                    "state='AI_UNAVAILABLE',safe_error_code='CONTEXT_EXPIRED',fence=fence+1,lease_until=NULL,lease_owner=NULL"
                    " WHERE organization_id=:org AND orchestration_run_id=:id AND state IN "
                    "('QUEUED','RUNNING')"
                ),
                {"org": self.organization_id, "id": locator.owner_id},
            )
        if locator.storage_kind == "workflow_runs":
            await self.session.execute(
                text(
                    "UPDATE workflow_runs SET status='FAILED',error_message='CONTEXT_EXPIRED' "
                    "WHERE organization_id=:org AND id=:id "
                    "AND status NOT IN ('WAITING_FOR_DECISION','COMPLETED','FAILED')"
                ),
                {"org": self.organization_id, "id": locator.owner_id},
            )
        if locator.storage_kind in (
            "assistant_turns",
            "agent_runs",
            "skill_invocations",
            "tool_invocations",
        ):
            await self.session.execute(
                text(
                    f"UPDATE {locator.storage_kind} SET status='FAILED',"
                    "safe_error_code='CONTEXT_EXPIRED' "
                    "WHERE organization_id=:org AND id=:id "
                    "AND status IN ('QUEUED','RUNNING','AWAITING_INPUT','AWAITING_HUMAN')"
                ),
                {"org": self.organization_id, "id": locator.owner_id},
            )
        if locator.storage_kind in ("assistant_jobs", "workflow_jobs"):
            worker = (
                "locked_by" if locator.storage_kind == "assistant_jobs" else "locked_by_worker_id"
            )
            error = "safe_error_code" if locator.storage_kind == "assistant_jobs" else "last_error"
            await self.session.execute(
                text(
                    f"UPDATE {locator.storage_kind} SET status='FAILED',"
                    f"{error}='CONTEXT_EXPIRED',{worker}=NULL,lease_until=NULL "
                    "WHERE organization_id=:org AND id=:id AND status IN ('QUEUED','RUNNING')"
                ),
                {"org": self.organization_id, "id": locator.owner_id},
            )

    async def evidence(self, count: int, now: datetime) -> None:
        batch = uuid4()
        payload = json.dumps(
            {
                "schema_version": "1.0",
                "id": str(batch),
                "purged": count,
                "policy_version": "ai-retention.v1",
            }
        )
        await self.session.execute(
            text(
                "INSERT INTO "
                "audit_events(id,organization_id,action,outcome,resource_type,resource_id,request_id,after_data,occurred_at)"
                " VALUES "
                "(:id,:org,'ai.retention.purged','SUCCEEDED','ai_retention',:id,:request,CAST(:counts"
                " AS jsonb),:now)"
            ),
            {
                "id": batch,
                "org": self.organization_id,
                "request": str(batch),
                "counts": json.dumps({"purged": count}),
                "now": now,
            },
        )
        await self.session.execute(
            text(
                "INSERT INTO "
                "outbox_events(id,event_id,organization_id,event_type,aggregate_type,aggregate_id,payload)"
                " VALUES "
                "(:id,:id,:org,'ai.retention.completed.v1','ai_retention',:batch,CAST(:payload"
                " AS jsonb))"
            ),
            {"id": uuid4(), "batch": batch, "org": self.organization_id, "payload": payload},
        )


class RetentionTransactions:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self.sessions = sessions

    @asynccontextmanager
    async def __call__(self, organization_id: UUID) -> AsyncGenerator[SQLRetentionRepository]:
        async with self.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE app_retention"))
            await session.execute(text("SET LOCAL statement_timeout='15s'"))
            await session.execute(text("SET LOCAL lock_timeout='1s'"))
            await session.execute(
                text("SELECT set_config('app.organization_id',:org,true)"),
                {"org": str(organization_id)},
            )
            await session.execute(
                text("SELECT set_config('app.ai_raw_retention_days',:days,true)"),
                {"days": str(get_settings().ai_raw_context_retention_days)},
            )
            yield SQLRetentionRepository(session, organization_id)
