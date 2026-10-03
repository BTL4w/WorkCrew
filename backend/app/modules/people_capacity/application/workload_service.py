"""Workload application service and data-loading protocol."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.people_capacity.domain.workload import (
    WeeklyWorkload,
    WorkloadInput,
    calculate_weekly_workload,
)


class WorkloadSource(Protocol):
    """Authoritative source for resolving raw capacity, leave, and task effort."""

    async def load_workload_inputs(
        self,
        *,
        actor: AuthenticatedActor,
        week_start: date,
        membership_id: UUID | None,
    ) -> tuple[WorkloadInput, ...]: ...


class WorkloadService:
    """Computes deterministic weekly workload projections from verified inputs."""

    def __init__(self, workload_source: WorkloadSource) -> None:
        self._workload_source = workload_source

    async def list_weekly_workload(
        self,
        *,
        actor: AuthenticatedActor,
        week_start: date,
        membership_id: UUID | None,
    ) -> tuple[WeeklyWorkload, ...]:
        inputs = await self._workload_source.load_workload_inputs(
            actor=actor,
            week_start=week_start,
            membership_id=membership_id,
        )
        return tuple(calculate_weekly_workload(item) for item in inputs)


@dataclass(frozen=True)
class RemainingWorkInput:
    weekly_capacity: Decimal | None
    leave_hours: Decimal
    remaining_days: int
    efforts: tuple[Decimal | None, ...]


@dataclass(frozen=True)
class RemainingWork:
    available_hours: Decimal | None
    known_hours: Decimal
    unknown_count: int
    overload: bool | None


def remaining_work(value: RemainingWorkInput) -> RemainingWork:
    if (
        not 0 <= value.remaining_days <= 5
        or not value.leave_hours.is_finite()
        or value.leave_hours < 0
    ):
        raise ValueError("Invalid remaining-work inputs")
    if value.weekly_capacity is not None and (
        not value.weekly_capacity.is_finite() or value.weekly_capacity < 0
    ):
        raise ValueError("Invalid capacity")
    if any(v is not None and (not v.is_finite() or v < 0) for v in value.efforts):
        raise ValueError("Invalid effort")
    available = (
        None
        if value.weekly_capacity is None
        else max(value.weekly_capacity - value.leave_hours, Decimal(0))
        * Decimal(value.remaining_days)
        / 5
    )
    known = sum((v for v in value.efforts if v is not None), Decimal(0))
    unknown = sum(v is None for v in value.efforts)
    overload = (
        None if available is None else True if known > available else None if unknown else False
    )
    return RemainingWork(available, known, unknown, overload)


class RemainingWorkSource(Protocol):
    async def load_remaining_work(
        self, *, actor: AuthenticatedActor, membership_id: UUID, observed_on: date
    ) -> RemainingWorkInput: ...
