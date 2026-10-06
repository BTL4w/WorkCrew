"""Fenced report-chat reservations in existing tenant-owned operational run state."""

from collections.abc import Generator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.assistant.domain.models import AssistantJob
from work_management_ai.agents.reporting.contracts import ReportingUsage
from work_management_ai.runtime.contracts import AgentHandoff

from .database_models import AgentRunModel, AssistantJobModel

_JOB: ContextVar[AssistantJob | None] = ContextVar("report_chat_job", default=None)


@contextmanager
def report_chat_job_scope(job: AssistantJob) -> Generator[None]:
    token = _JOB.set(job)
    try:
        yield
    finally:
        _JOB.reset(token)


class ReportChatUsageFactory:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self.sessions = sessions

    def __call__(self, handoff: AgentHandoff) -> "ReportChatUsage":
        job = _JOB.get()
        if job is None:
            raise ValueError("REPORT_CHAT_JOB_REQUIRED")
        return ReportChatUsage(self.sessions, job, handoff)


class ReportChatUsage:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], job: AssistantJob, handoff: AgentHandoff
    ):
        self.sessions, self.job, self.handoff = sessions, job, handoff
        self.run_id = uuid5(NAMESPACE_URL, f"agent-run:{handoff.idempotency_key}")

    @asynccontextmanager
    async def _row(self):
        actor = self.handoff.actor
        async with self.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true),"
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            at = cast(datetime, await session.scalar(text("SELECT clock_timestamp()")))
            job = await session.scalar(
                select(AssistantJobModel)
                .where(
                    AssistantJobModel.organization_id == actor.organization_id,
                    AssistantJobModel.id == self.job.id,
                )
                .with_for_update()
            )
            if (
                job is None
                or job.status != "RUNNING"
                or job.locked_by != self.job.locked_by
                or job.attempt_count != self.job.attempt_count
                or job.lease_until is None
                or job.lease_until <= at
                or job.requester_membership_id != actor.membership_id
            ):
                raise ValueError("REPORT_CHAT_STALE_CLAIM")
            active = await session.scalar(
                text(
                    "SELECT public.lock_active_membership(:org,:member) "
                    "AND EXISTS(SELECT 1 FROM memberships "
                    "WHERE organization_id=:org AND id=:member "
                    "AND role IN ('MANAGER','ADMIN'))"
                ),
                {"org": actor.organization_id, "member": actor.membership_id},
            )
            if active is not True:
                raise ValueError("REPORT_CHAT_ACTOR_UNAVAILABLE")
            run = await session.scalar(
                select(AgentRunModel)
                .where(
                    AgentRunModel.organization_id == actor.organization_id,
                    AgentRunModel.id == self.run_id,
                    AgentRunModel.agent_id == "reporting",
                    AgentRunModel.orchestration_run_id == job.orchestration_run_id,
                )
                .with_for_update()
            )
            if run is None or run.status != "RUNNING":
                raise ValueError("REPORT_CHAT_RUN_REQUIRED")
            at = cast(datetime, await session.scalar(text("SELECT clock_timestamp()")))
            if job.lease_until <= at:
                raise ValueError("REPORT_CHAT_STALE_CLAIM")
            values: dict[str, Any] = dict(run.usage.get("reporting_chat", {}))
            if not values:
                values = dict(
                    attempts=0,
                    input_reserved=0,
                    output_reserved=0,
                    tools=0,
                    retries=0,
                    deadline=(
                        at + timedelta(seconds=min(180, self.handoff.budget.timeout_seconds))
                    ).isoformat(),
                )
            deadline = datetime.fromisoformat(values["deadline"])
            if deadline <= at:
                raise ValueError("REPORT_CHAT_DEADLINE")
            # Cover the bounded specialist plus final persistence; never extend its deadline.
            job.lease_until = max(job.lease_until, deadline + timedelta(seconds=30))
            yield values, max(0.0, (deadline - at).total_seconds())
            ReportingUsage.model_validate({key: values[key] for key in ReportingUsage.model_fields})
            run.usage = {**run.usage, "reporting_chat": values}

    async def load(self) -> ReportingUsage:
        async with self._row() as (values, _):
            return ReportingUsage.model_validate(
                {key: values[key] for key in ReportingUsage.model_fields}
            )

    async def remaining_seconds(self) -> float:
        async with self._row() as (_, seconds):
            return seconds

    async def reserve(self, *, input_tokens: int, output_tokens: int) -> int:
        if (
            type(input_tokens) is not int
            or type(output_tokens) is not int
            or min(input_tokens, output_tokens) < 1
        ):
            raise ValueError("REPORT_CHAT_RESERVATION")
        async with self._row() as (values, _):
            budget = self.handoff.budget
            if (
                values["attempts"] >= min(3, budget.max_model_attempts)
                or values["input_reserved"] + input_tokens > min(48000, budget.max_input_tokens)
                or values["output_reserved"] + output_tokens > min(8000, budget.max_output_tokens)
            ):
                raise ValueError("REPORT_CHAT_MODEL_BUDGET")
            values["attempts"] += 1
            values["input_reserved"] += input_tokens
            values["output_reserved"] += output_tokens
            return values["attempts"]

    async def reserve_tool(self, invocation_key: str) -> None:
        async with self._row() as (values, _):
            if values["tools"] >= min(6, self.handoff.budget.max_tool_calls):
                raise ValueError("REPORT_CHAT_TOOL_BUDGET")
            values["tools"] += 1

    async def consume_retry(self) -> None:
        async with self._row() as (values, _):
            if values["retries"] >= 1:
                raise ValueError("REPORT_CHAT_RETRY_BUDGET")
            values["retries"] += 1
