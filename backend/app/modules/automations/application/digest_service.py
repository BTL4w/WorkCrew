"""Idempotent factual capture and leased in-app delivery through application transactions."""

import logging
from typing import Literal
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.progress.domain.daily_updates import DailyUpdateError

from ..domain.digests import AuthorizedJobScope, DailySummarySnapshot, SummaryDelivery
from ..domain.schedules import ScheduleError
from .digest_ports import DigestTransactionPort

logger = logging.getLogger(__name__)


class DigestService:
    def __init__(self, transactions: DigestTransactionPort):
        self.transactions = transactions

    async def trigger(
        self,
        scope: AuthorizedJobScope,
        window_id: UUID,
        reason: Literal["COVERAGE", "CUTOFF", "RESUME"],
    ) -> DailySummarySnapshot:
        try:
            async with self.transactions(scope.actor) as repo:
                await repo.authenticate()
                schedule = await repo.schedule_for_window(window_id)
                await repo.authorize(schedule.project_id)
                schedule = await repo.schedule_for_window(window_id)
                if schedule.paused:
                    raise ScheduleError("SCHEDULE_PAUSED")
                window = await repo.window(schedule, scope.at)
                if window.id != window_id or not window.enabled:
                    raise ScheduleError("WINDOW_NOT_ACTIVE")
                prior = await repo.existing(window_id)
                if prior:
                    return prior
                applied = await repo.applied(schedule, window.applied_version)
                coverage_due = window.full_coverage and applied.send_when_complete
                cutoff_due = scope.at >= window.cutoff_at and applied.partial_at_cutoff
                if reason == "COVERAGE" and not coverage_due:
                    raise ScheduleError("TRIGGER_NOT_DUE")
                if reason in {"CUTOFF", "RESUME"} and not cutoff_due:
                    raise ScheduleError("TRIGGER_NOT_DUE")
                snapshot = await repo.capture(schedule, window, scope.at, reason)
                await repo.create(snapshot)
                return snapshot
        except DailyUpdateError as exc:
            raise ScheduleError(exc.code, exc.status) from exc

    async def deliver(self, scope: AuthorizedJobScope, *, worker_id: str) -> bool:
        try:
            async with self.transactions(scope.actor) as repo:
                await repo.authenticate()
                delivery_id = await repo.claim(scope.at, worker_id)
            if delivery_id is None:
                return False
            try:
                async with self.transactions(scope.actor) as repo:
                    await repo.complete_delivery(delivery_id, scope.at, worker_id)
            except ScheduleError:
                raise
            except Exception:
                logger.exception("Daily summary delivery failed: %s", delivery_id)
                async with self.transactions(scope.actor) as repo:
                    await repo.authenticate()
                    await repo.fail_delivery(delivery_id, scope.at, worker_id)
            return True
        except DailyUpdateError as exc:
            raise ScheduleError(exc.code, exc.status) from exc

    async def list(self, actor: AuthenticatedActor) -> tuple[SummaryDelivery, ...]:
        try:
            async with self.transactions(actor) as repo:
                await repo.authenticate_viewer()
                return await repo.feed()
        except DailyUpdateError as exc:
            raise ScheduleError(exc.code, exc.status) from exc
