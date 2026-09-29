"""Manager-scoped immutable baseline and current-plan comparisons."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.progress.application.progress_ports import ProgressTransactionFactory
from app.modules.progress.domain.weekly_progress import WeekActuals, aggregate_week


class ProgressForbiddenError(Exception):
    pass


class ProgressNotFoundError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class WeeklyProgress:
    original: WeekActuals
    current_plan: WeekActuals
    added_task_ids: tuple[UUID, ...]
    removed_task_ids: tuple[UUID, ...]
    sealed: bool


class WeeklyProgressService:
    def __init__(self, transactions: ProgressTransactionFactory):
        self.transactions = transactions

    async def get(
        self, actor: AuthenticatedActor, project_id: UUID, week_id: UUID, at: datetime
    ) -> WeeklyProgress:
        if actor.role not in (MembershipRole.MANAGER, MembershipRole.ADMIN):
            raise ProgressForbiddenError
        async with self.transactions(actor) as repository:
            result = await repository.get(project_id, week_id)
            if result is None:
                raise ProgressNotFoundError
            original, current, actuals = result
            original_ids = {item.task_id for item in original.task_entries}
            current_ids = {item.task_id for item in current.task_entries}
            evaluated_at = current.sealed_at or at
            return WeeklyProgress(
                aggregate_week(original, actuals, evaluated_at),
                aggregate_week(current, actuals, evaluated_at),
                tuple(sorted(current_ids - original_ids)),
                tuple(sorted(original_ids - current_ids)),
                current.sealed_at is not None,
            )
