"""Current-authorized, exact-version preview/confirmation and immediate pause."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.progress.domain.daily_updates import DailyUpdateError

from ..domain.schedules import (
    Contract,
    DailySummarySchedule,
    Reporter,
    ReportingWindow,
    ScheduleCommand,
    ScheduleDraft,
    ScheduleError,
)
from .ports import ScheduleTransactionPort


class ScheduleView(Contract):
    schedule: DailySummarySchedule | None
    window: ReportingWindow | None
    recipients: tuple[Reporter, ...]


class ScheduleService:
    def __init__(
        self, transactions: ScheduleTransactionPort, *, clock: Callable[[], datetime] | None = None
    ):
        self.transactions = transactions
        self.clock = clock or (lambda: datetime.now(UTC))

    async def audit_transport_rejection(
        self, actor: AuthenticatedActor, key: str, request_id: str, **kwargs: object
    ) -> None:
        async with self.transactions(actor) as repo:
            await repo.record_audit(
                "schedule.transport_rejected", key or request_id, None, "INVALID_REQUEST"
            )

    async def get(self, actor: AuthenticatedActor, project_id: UUID) -> ScheduleView:
        try:
            async with self.transactions(actor) as repo:
                await repo.authenticate()
                await repo.authorize(project_id)
                current = await repo.current(project_id)
                return ScheduleView(
                    schedule=current,
                    window=await repo.window(current, self.clock()) if current else None,
                    recipients=await repo.recipients(project_id),
                )
        except DailyUpdateError as exc:
            raise ScheduleError(exc.code, exc.status) from exc

    async def preview(
        self, actor: AuthenticatedActor, command: ScheduleCommand, expected_version: int, key: str
    ) -> ScheduleDraft:
        async def operation() -> ScheduleDraft:
            async with self.transactions(actor) as repo:
                await repo.authenticate()
                await repo.authorize(command.project_id)
                current = await repo.current(command.project_id)
                if (current.version if current else 0) != expected_version:
                    raise ScheduleError("STALE_SCHEDULE")
                allowed = {r.membership_id for r in await repo.recipients(command.project_id)}
                if not set(command.recipients) <= allowed:
                    raise ScheduleError("INVALID_RECIPIENT", 422)
                fingerprint = digest(
                    {"command": command.model_dump(mode="json"), "version": expected_version}
                )
                replay = await repo.replay("schedule.preview", key, fingerprint)
                if replay:
                    return ScheduleDraft.model_validate(replay)
                now = self.clock()
                effective = (await repo.window(current, now)).ends_at if current else now
                draft = ScheduleDraft(
                    id=uuid4(),
                    command=command,
                    expected_version=expected_version,
                    expires_at=now + timedelta(minutes=15),
                    effective_at=effective,
                )
                await repo.store_preview(draft)
                await repo.remember(
                    "schedule.preview", key, fingerprint, draft.model_dump(mode="json")
                )
                await repo.record_audit("schedule.previewed", key, command.project_id)
                return draft

        return await self._mutation(actor, key, command.project_id, operation)

    async def confirm(
        self, actor: AuthenticatedActor, draft_id: UUID, expected_version: int, key: str
    ) -> DailySummarySchedule:
        async def operation() -> DailySummarySchedule:
            async with self.transactions(actor) as repo:
                await repo.authenticate()
                draft = await repo.preview_by_id(draft_id)
                await repo.authorize(draft.command.project_id)
                allowed = {r.membership_id for r in await repo.recipients(draft.command.project_id)}
                if not set(draft.command.recipients) <= allowed:
                    raise ScheduleError("INVALID_RECIPIENT", 422)
                fingerprint = digest({"draft": str(draft_id), "version": expected_version})
                replay = await repo.replay("schedule.confirm", key, fingerprint)
                if replay:
                    return DailySummarySchedule.model_validate(replay)
                current = await repo.current(draft.command.project_id)
                if (
                    draft.expected_version != expected_version
                    or (current.version if current else 0) != expected_version
                ):
                    raise ScheduleError("STALE_SCHEDULE")
                if self.clock() >= draft.expires_at:
                    raise ScheduleError("DRAFT_EXPIRED")
                # A preview that crossed its window boundary must be reviewed again.
                effective = (
                    (await repo.window(current, self.clock())).ends_at
                    if current
                    else draft.effective_at
                )
                if current and effective != draft.effective_at:
                    raise ScheduleError("STALE_WINDOW")
                result = DailySummarySchedule(
                    id=current.id if current else uuid4(),
                    version=expected_version + 1,
                    paused=current.paused if current else False,
                    effective_at=draft.effective_at,
                    **draft.command.model_dump(),
                )
                await repo.save(result)
                if current is None:
                    await repo.window(result, self.clock())
                await repo.remember(
                    "schedule.confirm", key, fingerprint, result.model_dump(mode="json")
                )
                await repo.record_audit("schedule.confirmed", key, result.id)
                return result

        return await self._mutation(actor, key, draft_id, operation)

    async def pause(
        self,
        actor: AuthenticatedActor,
        schedule_id: UUID,
        expected_version: int,
        paused: bool,
        key: str,
    ) -> DailySummarySchedule:
        async def operation() -> DailySummarySchedule:
            async with self.transactions(actor) as repo:
                await repo.authenticate()
                current = await repo.by_id(schedule_id)
                await repo.authorize(current.project_id)
                fingerprint = digest(
                    {"id": str(schedule_id), "version": expected_version, "paused": paused}
                )
                replay = await repo.replay("schedule.pause", key, fingerprint)
                if replay:
                    return DailySummarySchedule.model_validate(replay)
                if current.version != expected_version:
                    raise ScheduleError("STALE_SCHEDULE")
                result = current.model_copy(
                    update={"version": current.version + 1, "paused": paused}
                )
                await repo.save(result)
                await repo.remember(
                    "schedule.pause", key, fingerprint, result.model_dump(mode="json")
                )
                await repo.record_audit(
                    "schedule.paused" if paused else "schedule.resumed", key, result.id
                )
                return result

        return await self._mutation(actor, key, schedule_id, operation)

    async def _mutation[T](
        self,
        actor: AuthenticatedActor,
        key: str,
        resource: UUID,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        try:
            if not 16 <= len(key) <= 128:
                raise ScheduleError("IDEMPOTENCY_KEY_REQUIRED", 400)
            return await operation()
        except (ScheduleError, DailyUpdateError) as exc:
            async with self.transactions(actor) as repo:
                await repo.record_audit("schedule.rejected", key, resource, exc.code)
            raise ScheduleError(exc.code, exc.status) from exc


def digest(value: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
