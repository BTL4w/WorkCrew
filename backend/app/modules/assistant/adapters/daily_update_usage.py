"""Atomic, tenant-scoped run and original-version usage reservations."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from work_management_ai.runtime.daily_update_budget import BudgetScope, UsageLimitExceeded


class SqlAlchemyDailyUsageStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def remaining_seconds(self, scope: BudgetScope) -> float:
        async with self.sessions.begin() as session:
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true), "
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(scope.organization_id), "member": str(scope.membership_id)},
            )
            value = await session.scalar(
                text(
                    "SELECT deadline FROM agent_usage_budgets "
                    "WHERE organization_id=:org AND owner_membership_id=:member "
                    "AND resource_id=:id AND version=0"
                ),
                {"org": scope.organization_id, "member": scope.membership_id, "id": scope.run_id},
            )
            deadline = value or (
                scope.started_at + timedelta(seconds=min(180, scope.timeout_seconds))
            )
            return max(0.0, (deadline - datetime.now(UTC)).total_seconds())

    async def run_attempts(self, scope: BudgetScope) -> int:
        async with self.sessions.begin() as session:
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true), "
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(scope.organization_id), "member": str(scope.membership_id)},
            )
            value = await session.scalar(
                text(
                    "SELECT attempts FROM agent_usage_budgets "
                    "WHERE organization_id=:org AND owner_membership_id=:member "
                    "AND resource_id=:id AND version=0"
                ),
                {"org": scope.organization_id, "member": scope.membership_id, "id": scope.run_id},
            )
            return int(value or 0)

    async def reserve(self, scope: BudgetScope, *, input_tokens: int, output_tokens: int) -> int:
        if input_tokens <= 0 or output_tokens <= 0:
            raise UsageLimitExceeded("INVALID_USAGE_RESERVATION")
        now = datetime.now(UTC)
        async with self.sessions.begin() as session:
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true), "
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(scope.organization_id), "member": str(scope.membership_id)},
            )
            run_attempt = await self._reserve(
                session,
                scope,
                "agent_usage_budgets",
                scope.run_id,
                0,
                input_tokens,
                output_tokens,
                min(3, scope.max_model_attempts),
                24000,
                4000,
                now,
            )
            for identifier, version in sorted(set(scope.evidence_versions)):
                await self._reserve(
                    session,
                    scope,
                    "evidence_model_budgets",
                    identifier,
                    version,
                    input_tokens,
                    output_tokens,
                    10,
                    240000,
                    40000,
                    now,
                )

        return run_attempt

    @staticmethod
    async def _reserve(
        session: AsyncSession,
        scope: BudgetScope,
        table: str,
        identifier: UUID,
        version: int,
        inputs: int,
        outputs: int,
        attempts: int,
        max_inputs: int,
        max_outputs: int,
        now: datetime,
    ) -> int:
        # Identifiers come exclusively from this module, never model/client text.
        parameters = {
            "org": scope.organization_id,
            "member": scope.membership_id,
            "id": identifier,
            "version": version,
            "inputs": inputs,
            "outputs": outputs,
            "attempts": attempts,
            "max_inputs": max_inputs,
            "max_outputs": max_outputs,
            "now": now,
            "deadline": scope.started_at + timedelta(seconds=min(180, scope.timeout_seconds)),
        }
        await session.execute(
            text(
                f"INSERT INTO {table} "
                "(organization_id, owner_membership_id, resource_id, version, "
                "attempts, input_tokens, "
                "output_tokens, deadline) VALUES (:org,:member,:id,:version,0,0,0,:deadline) "
                "ON CONFLICT (organization_id,owner_membership_id,resource_id,version) DO NOTHING"
            ),
            parameters,
        )
        deadline = " AND deadline >= :now" if table == "agent_usage_budgets" else ""
        updated = await session.scalar(
            text(
                f"UPDATE {table} SET attempts=attempts+1, "
                "input_tokens=input_tokens+:inputs, output_tokens=output_tokens+:outputs "
                "WHERE organization_id=:org AND owner_membership_id=:member AND resource_id=:id "
                "AND version=:version AND attempts<:attempts AND input_tokens+:inputs<=:max_inputs "
                f"AND output_tokens+:outputs<=:max_outputs{deadline} RETURNING attempts"
            ),
            parameters,
        )
        if updated is None:
            raise UsageLimitExceeded("DAILY_UPDATE_MODEL_BUDGET_EXHAUSTED")
        return int(updated)
