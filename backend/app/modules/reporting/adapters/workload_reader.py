"""Capture Phase 3 workload for assigned members, without inventing missing capacity."""

from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.people_capacity.adapters.database_models import CapacityEntryModel, LeaveEntryModel
from app.modules.people_capacity.adapters.repository import SqlAlchemyPeopleCapacityRepository
from app.modules.people_capacity.domain.availability import CapacityKind
from app.modules.people_capacity.domain.workload import calculate_weekly_workload
from app.modules.work.adapters.database_models import TaskModel
from app.modules.work.planning.adapters.database_models import ProjectWeekModel

from ..domain.catalog import metric
from ..domain.commands import CaptureReportCommand
from ..domain.metrics import MetricState, MetricValue, SourceRef
from ..domain.snapshots import canonical_hash


async def capture_workload(
    session: AsyncSession,
    actor: AuthenticatedActor,
    command: CaptureReportCommand,
    weeks: tuple[ProjectWeekModel, ...],
    tasks: tuple[TaskModel, ...],
) -> tuple[dict[str, MetricValue], tuple[SourceRef, ...]]:
    values: dict[str, MetricValue] = {}
    refs: dict[tuple[str, UUID], SourceRef] = {}
    org = actor.organization_id
    repo = SqlAlchemyPeopleCapacityRepository(session)
    known_capacity = 0
    unknown_count = 0
    pair_count = 0
    for start in sorted({w.start_date for w in weeks}):
        scoped_weeks = {w.id: w for w in weeks if w.start_date == start}
        inputs = await repo.load_workload_inputs(actor=actor, week_start=start, membership_id=None)
        target_pairs = {
            (t.project_week_id, t.assignee_membership_id)
            for t in tasks
            if t.project_week_id in scoped_weeks and t.assignee_membership_id
        }
        target_ids = {member for _, member in target_pairs}
        available_pairs = {(i.project_week_id, i.membership_id) for i in inputs}
        missing_pairs = target_pairs - available_pairs
        pair_count += len(missing_pairs)
        unknown_count += len(missing_pairs)
        for week_id, member in missing_pairs:
            for suffix, unit in (
                ("capacity_hours", "HOURS"),
                ("allocated_hours", "HOURS"),
                ("residual_hours", "HOURS"),
                ("ratio", "FRACTION"),
            ):
                key = f"workload.{week_id}.{member}.{suffix}"
                values[key] = metric(
                    key,
                    None,
                    unit=unit,
                    policy="phase3-weekly-workload.v1",
                    limitations=("MEMBER_UNAVAILABLE",),
                )
        all_weeks = tuple(
            await session.scalars(
                select(ProjectWeekModel).where(
                    ProjectWeekModel.organization_id == org, ProjectWeekModel.start_date == start
                )
            )
        )
        allocations = tuple(
            await session.scalars(
                select(TaskModel).where(
                    TaskModel.organization_id == org,
                    TaskModel.project_week_id.in_([w.id for w in all_weeks]),
                    TaskModel.assignee_membership_id.in_(target_ids),
                    TaskModel.status.in_(("TO_DO", "IN_PROGRESS")),
                )
            )
        )
        capacities = tuple(
            await session.scalars(
                select(CapacityEntryModel).where(
                    CapacityEntryModel.organization_id == org,
                    CapacityEntryModel.membership_id.in_(target_ids),
                )
            )
        )
        leaves = tuple(
            await session.scalars(
                select(LeaveEntryModel).where(
                    LeaveEntryModel.organization_id == org,
                    LeaveEntryModel.membership_id.in_(target_ids),
                    LeaveEntryModel.start_date <= max(w.end_date for w in scoped_weeks.values()),
                    LeaveEntryModel.end_date >= start,
                )
            )
        )
        for item in inputs:
            if (item.project_week_id, item.membership_id) not in target_pairs:
                continue
            week = scoped_weeks[item.project_week_id]
            pair_count += 1
            member_capacity = tuple(
                c
                for c in capacities
                if c.membership_id == item.membership_id
                and (
                    (
                        c.kind == CapacityKind.DEFAULT
                        and c.effective_from <= week.end_date
                        and c.effective_to >= start
                    )
                    or (c.kind == CapacityKind.OVERRIDE and c.week_start == start)
                )
            )
            member_leaves = tuple(
                leave
                for leave in leaves
                if leave.membership_id == item.membership_id
                and leave.start_date <= week.end_date
                and leave.end_date >= start
            )
            member_tasks = tuple(
                t for t in allocations if t.assignee_membership_id == item.membership_id
            )
            result = calculate_weekly_workload(item)
            capacity_known = bool(member_capacity)
            missing_estimates = sum(t.estimated_effort_hours is None for t in member_tasks)
            if capacity_known:
                known_capacity += result.effective_capacity_hours
            else:
                unknown_count += 1
            local_refs: list[SourceRef] = []
            for c in member_capacity:
                facts: dict[str, JsonValue] = {
                    "membership_id": str(c.membership_id),
                    "kind": c.kind.value,
                    "hours": c.hours,
                    "effective_from": c.effective_from.isoformat(),
                    "effective_to": c.effective_to.isoformat(),
                    "week_start": c.week_start.isoformat() if c.week_start else None,
                }
                ref = SourceRef(
                    resource_type="CAPACITY",
                    resource_id=c.id,
                    version=c.version,
                    observed_at=command.captured_at,
                    label=str(c.membership_id),
                    facts=facts,
                    fingerprint=canonical_hash(facts),
                )
                refs[(ref.resource_type, ref.resource_id)] = ref
                local_refs.append(ref)
            for leave in member_leaves:
                facts: dict[str, JsonValue] = {
                    "membership_id": str(leave.membership_id),
                    "start_date": leave.start_date.isoformat(),
                    "end_date": leave.end_date.isoformat(),
                    "unavailable_hours": leave.unavailable_hours,
                }
                ref = SourceRef(
                    resource_type="LEAVE",
                    resource_id=leave.id,
                    version=leave.version,
                    observed_at=command.captured_at,
                    label=str(leave.membership_id),
                    facts=facts,
                    fingerprint=canonical_hash(facts),
                )
                refs[(ref.resource_type, ref.resource_id)] = ref
                local_refs.append(ref)
            for task in member_tasks:
                facts: dict[str, JsonValue] = {
                    "status": task.status.value,
                    "project_id": str(task.project_id),
                    "project_week_id": str(task.project_week_id),
                    "estimated_effort_hours": task.estimated_effort_hours,
                    "assignee_membership_id": str(task.assignee_membership_id),
                }
                ref = SourceRef(
                    resource_type="TASK",
                    resource_id=task.id,
                    version=task.version,
                    observed_at=command.captured_at,
                    label=task.title,
                    facts=facts,
                    fingerprint=canonical_hash(facts),
                )
                refs[(ref.resource_type, ref.resource_id)] = ref
                local_refs.append(ref)
            prefix = f"workload.{week.id}.{item.membership_id}"
            for suffix, value, unit in (
                (
                    "capacity_hours",
                    result.effective_capacity_hours if capacity_known else None,
                    "HOURS",
                ),
                ("allocated_known_subtotal_hours", result.allocated_effort_hours, "HOURS"),
                (
                    "allocated_hours",
                    result.allocated_effort_hours if not missing_estimates else None,
                    "HOURS",
                ),
                (
                    "residual_hours",
                    result.residual_capacity_hours
                    if capacity_known and not missing_estimates
                    else None,
                    "HOURS",
                ),
                (
                    "ratio",
                    result.workload_ratio if capacity_known and not missing_estimates else None,
                    "FRACTION",
                ),
                ("missing_estimate_count", missing_estimates, "COUNT"),
            ):
                key = f"{prefix}.{suffix}"
                values[key] = metric(
                    key,
                    value,
                    unit=unit,
                    policy="phase3-weekly-workload.v1",
                    limitations=("ORGANIZATION_WEEK_ALLOCATION",)
                    + (("CAPACITY_UNAVAILABLE",) if not capacity_known else ())
                    + (("INCOMPLETE_ESTIMATES",) if missing_estimates else ()),
                ).model_copy(update={"source_refs": tuple(local_refs)})
    values["workload.capacity_hours"] = metric(
        "workload.capacity_hours",
        known_capacity if pair_count and not unknown_count else None,
        unit="HOURS",
        state=MetricState.NOT_APPLICABLE if not pair_count else None,
        limitations=("PER_MEMBER_WEEK_CAPACITY",),
    )
    values["workload.capacity_known_subtotal_hours"] = metric(
        "workload.capacity_known_subtotal_hours", known_capacity, unit="HOURS"
    )
    values["workload.capacity_unknown_count"] = metric(
        "workload.capacity_unknown_count", unknown_count
    )
    return values, tuple(refs.values())
