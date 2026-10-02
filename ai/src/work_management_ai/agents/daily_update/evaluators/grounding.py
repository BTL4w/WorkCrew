"""Deterministic authority and context verifier."""

from work_management_ai.agents.daily_update.contracts import DailyUpdateHandoff, ReportingSnapshot


def verify_context(value: DailyUpdateHandoff, snapshot: ReportingSnapshot) -> None:
    if snapshot.task_id != value.task_id or snapshot.task_version != value.task_version:
        raise ValueError("DAILY_UPDATE_STALE_TASK")
