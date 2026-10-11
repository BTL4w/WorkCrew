"""Resolve relative schedules once; confirmations never recalculate their anchor."""

from datetime import date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from work_management_ai.schemas.planning import (
    PlanningModelOutput,
    ProposedAssumption,
    ProposedDependency,
    ProposedGoal,
    ProposedMilestone,
    ProposedProject,
    ProposedProjectWeek,
    ProposedTask,
)
from work_management_ai.schemas.progressive_planning import PlanningDraft
from work_management_ai.workflows.planning.draft_verifier import (
    DraftVerificationContext,
    planning_source_references,
    verify_draft,
)


def materialize_draft(
    draft: PlanningDraft, anchor_date: date, timezone: str
) -> PlanningModelOutput:
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("invalid planning timezone") from error
    refs = planning_source_references(draft)
    validation = verify_draft(
        draft, DraftVerificationContext(permitted_source_refs=frozenset(refs))
    )
    if not validation.can_prepare:
        raise ValueError(
            ",".join(
                [item.code for item in validation.errors]
                + [item.code for item in validation.conflicts]
            )
        )
    end = anchor_date + timedelta(days=draft.duration_days - 1)
    objective = str(draft.brief.objective.value)
    weeks = [
        ProposedProjectWeek(
            ref=f"week-{start // 7 + 1}",
            week_number=start // 7 + 1,
            start_date=anchor_date + timedelta(days=start),
            end_date=anchor_date + timedelta(days=min(start + 7, draft.duration_days) - 1),
            objective="; ".join(
                p.title for p in draft.phases if p.start_day < start + 7 and p.end_day > start
            )
            or objective,
        )
        for start in range(0, draft.duration_days, 7)
    ]
    assumptions = [
        ProposedAssumption(
            description=str(item.value), source=item.assumption_reason or "AI planning assumption"
        )
        for item in draft.assumptions
    ]
    for name, field in (("Audience", draft.brief.audience), ("Timing", draft.brief.timing)):
        if field.status == "AI_ASSUMPTION":
            assumptions.append(
                ProposedAssumption(
                    description=f"{name}: {field.value}",
                    source=field.assumption_reason or "AI planning assumption",
                )
            )
    if draft.brief.budget is not None:
        budget = draft.brief.budget
        assumptions.append(
            ProposedAssumption(
                description=(
                    f"Planning budget: {budget.currency} {budget.amount}; "
                    f"{budget.status.value}. "
                    "This is a planning constraint, not approved funding."
                ),
                source=budget.assumption_reason or "User-provided planning constraint",
            )
        )
    assumptions.append(
        ProposedAssumption(
            description=f"Calendar anchor: {anchor_date.isoformat()} ({timezone})",
            source="Manager-selected preparation date; fixed for this proposal",
        )
    )
    return PlanningModelOutput(
        project=ProposedProject(
            title=objective,
            description=f"Audience: {draft.brief.audience.value or 'Unknown'}",
            start_date=anchor_date,
            due_date=end,
        ),
        goal=ProposedGoal(
            title=objective,
            description=None,
            expected_outcomes=[str(item.value) for item in draft.brief.expected_outcomes],
            target_date=end,
        ),
        milestones=[
            ProposedMilestone(
                ref=p.ref,
                title=p.title,
                description=None,
                due_date=anchor_date + timedelta(days=p.end_day - 1),
            )
            for p in draft.phases
        ],
        project_weeks=weeks,
        tasks=[
            ProposedTask(
                ref=t.ref,
                project_week_ref=f"week-{(t.end_day - 1) // 7 + 1}",
                milestone_ref=t.phase_ref,
                title=t.title,
                description=t.description,
                due_date=anchor_date + timedelta(days=t.end_day - 1),
                required_skill_labels=list(t.required_skill_labels),
                estimated_effort_hours=t.estimated_effort_hours,
                acceptance_criteria=list(t.acceptance_criteria),
            )
            for t in draft.tasks
        ],
        dependencies=[
            ProposedDependency(predecessor_ref=d.predecessor_ref, successor_ref=d.successor_ref)
            for d in draft.dependencies
        ],
        assumptions=assumptions,
    )
