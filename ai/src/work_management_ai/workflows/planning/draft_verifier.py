"""Deterministic source, constraint and critical-path checks for relative drafts."""

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

from work_management_ai.schemas.progressive_planning import (
    DraftBinding,
    PlanningChangeAssessment,
    PlanningDecision,
    PlanningDraft,
    PlanningField,
)

DRAFT_VERIFIER_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class DraftVerificationContext:
    permitted_source_refs: frozenset[str] = frozenset()
    trusted_constraints: Mapping[str, PlanningField] = field(
        default_factory=lambda: dict[str, PlanningField]()
    )
    binding: DraftBinding | None = None


@dataclass(frozen=True, slots=True)
class DraftFinding:
    path: str
    code: str


@dataclass(frozen=True, slots=True)
class DraftConflict:
    code: str
    requested_days: int
    minimum_days: int
    chain_refs: tuple[str, ...]
    estimated: bool


@dataclass(frozen=True, slots=True)
class DraftValidationResult:
    errors: tuple[DraftFinding, ...] = ()
    warnings: tuple[DraftFinding, ...] = ()
    conflicts: tuple[DraftConflict, ...] = ()

    @property
    def can_prepare(self) -> bool:
        return not self.errors and not self.conflicts


def _fields(value: object, path: str = "") -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    if isinstance(value, dict):
        value = cast(dict[str, object], value)
        if "status" in value and ("value" in value or "amount" in value):
            result[path] = value
        for key, item in value.items():
            result.update(_fields(item, f"{path}.{key}" if path else key))
    elif isinstance(value, list):
        for index, item in enumerate(cast(list[object], value)):
            result.update(_fields(item, f"{path}[{index}]"))
    return result


def _source_errors(value: object, permitted: frozenset[str], path: str = "") -> list[DraftFinding]:
    errors: list[DraftFinding] = []
    if isinstance(value, dict):
        for key, item in cast(dict[str, object], value).items():
            item_path = f"{path}.{key}" if path else str(key)
            if key in {"source_refs", "duration_source_refs"} and isinstance(item, list):
                if any(ref not in permitted for ref in cast(list[object], item)):
                    errors.append(DraftFinding(item_path, "SOURCE_NOT_PERMITTED"))
            else:
                errors.extend(_source_errors(item, permitted, item_path))
    elif isinstance(value, list):
        for index, item in enumerate(cast(list[object], value)):
            errors.extend(_source_errors(item, permitted, f"{path}[{index}]"))
    return errors


def verify_draft(draft: PlanningDraft, context: DraftVerificationContext) -> DraftValidationResult:
    content = draft.model_dump(mode="json")
    errors = _source_errors(content, context.permitted_source_refs)
    if not draft.brief.objective.value or draft.brief.objective.status not in {
        "USER_STATED",
        "AUTHORIZED_SOURCE",
    }:
        errors.append(DraftFinding("brief.objective", "GOAL_REQUIRED"))
    for path, constraint in context.trusted_constraints.items():
        if _fields(content).get(path) != constraint.model_dump(mode="json"):
            errors.append(DraftFinding(path, "USER_CONSTRAINT_CHANGED"))
    phase_refs = {phase.ref for phase in draft.phases}
    tasks = {task.ref: task for task in draft.tasks}
    if len(tasks) != len(draft.tasks) or len(phase_refs) != len(draft.phases):
        errors.append(DraftFinding("tasks", "REF_DUPLICATE"))
    for phase in draft.phases:
        if phase.end_day <= phase.start_day:
            errors.append(DraftFinding(f"phases[{phase.ref}]", "PHASE_INTERVAL_INVALID"))
    for task in draft.tasks:
        if task.phase_ref not in phase_refs:
            errors.append(DraftFinding(f"tasks[{task.ref}]", "PHASE_REF_UNKNOWN"))
        else:
            phase = next(phase for phase in draft.phases if phase.ref == task.phase_ref)
            if task.start_day < phase.start_day or task.end_day > phase.end_day:
                errors.append(DraftFinding(f"tasks[{task.ref}]", "TASK_OUTSIDE_PHASE"))
        if task.end_day - task.start_day != task.duration_days:
            errors.append(DraftFinding(f"tasks[{task.ref}]", "TASK_INTERVAL_INVALID"))
        if task.duration_source != "AI_ASSUMPTION" and not task.duration_source_refs:
            errors.append(DraftFinding(f"tasks[{task.ref}]", "DURATION_SOURCE_REQUIRED"))
    predecessors: dict[str, list[str]] = {ref: [] for ref in tasks}
    successors: dict[str, list[str]] = {ref: [] for ref in tasks}
    seen: set[tuple[str, str]] = set()
    for edge in draft.dependencies:
        pair = (edge.predecessor_ref, edge.successor_ref)
        if pair in seen:
            errors.append(DraftFinding("dependencies", "DEPENDENCY_DUPLICATE"))
            continue
        seen.add(pair)
        if any(ref not in tasks for ref in pair):
            errors.append(DraftFinding("dependencies", "DEPENDENCY_REF_UNKNOWN"))
            continue
        predecessors[pair[1]].append(pair[0])
        successors[pair[0]].append(pair[1])
        if tasks[pair[0]].end_day > tasks[pair[1]].start_day:
            errors.append(DraftFinding(f"tasks[{pair[1]}]", "DEPENDENCY_SCHEDULE_CONFLICT"))
    incoming = {ref: len(preds) for ref, preds in predecessors.items()}
    queue = deque(ref for ref in tasks if incoming[ref] == 0)
    finish: dict[str, int] = {}
    chains: dict[str, tuple[str, ...]] = {}
    while queue:
        ref = queue.popleft()
        previous = max(predecessors[ref], key=lambda item: finish[item], default=None)
        finish[ref] = (finish[previous] if previous else 0) + tasks[ref].duration_days
        chains[ref] = (*(chains[previous] if previous else ()), ref)
        for successor in successors[ref]:
            incoming[successor] -= 1
            if incoming[successor] == 0:
                queue.append(successor)
    if len(finish) != len(tasks):
        errors.append(DraftFinding("dependencies", "DEPENDENCY_CYCLE"))
        return DraftValidationResult(errors=tuple(errors))
    conflicts: list[DraftConflict] = []
    longest = max(finish, key=lambda ref: finish[ref])
    if finish[longest] > draft.duration_days:
        chain = chains[longest]
        conflicts.append(
            DraftConflict(
                "HORIZON_DEPENDENCY_CONFLICT",
                draft.duration_days,
                finish[longest],
                chain,
                any(tasks[ref].duration_source == "AI_ASSUMPTION" for ref in chain),
            )
        )
    elif any(task.end_day > draft.duration_days for task in draft.tasks) or any(
        phase.end_day > draft.duration_days for phase in draft.phases
    ):
        errors.append(DraftFinding("duration_days", "SCHEDULE_OUTSIDE_HORIZON"))
    return DraftValidationResult(errors=tuple(errors), conflicts=tuple(conflicts))


def verify_decision(
    decision: PlanningDecision, context: DraftVerificationContext
) -> DraftValidationResult:
    if decision.draft is not None:
        return verify_draft(decision.draft, context)
    return DraftValidationResult(
        errors=tuple(
            _source_errors(decision.model_dump(mode="json"), context.permitted_source_refs)
        )
    )


def verify_draft_revision(
    base: PlanningDraft, candidate: PlanningDraft, assessment: PlanningChangeAssessment
) -> DraftValidationResult:
    old_fields = _fields(base.model_dump(mode="json"))
    new_fields = _fields(candidate.model_dump(mode="json"))
    errors: list[DraftFinding] = []
    for path, old in old_fields.items():
        if old.get("status") not in {"USER_STATED", "AUTHORIZED_SOURCE"}:
            continue
        new = new_fields.get(path)
        explicitly_changed = bool(assessment.source_refs) and any(
            path == changed or path.startswith(changed + ".")
            for changed in assessment.affected_field_paths
        )
        if new != old and (
            not explicitly_changed or new is None or new.get("status") != "USER_STATED"
        ):
            errors.append(DraftFinding(path, "USER_CONSTRAINT_CHANGED"))
    if base.duration_days != candidate.duration_days and not any(
        path in {"duration_days", "brief.timing"} for path in assessment.affected_field_paths
    ):
        errors.append(DraftFinding("duration_days", "UNREQUESTED_DURATION_CHANGE"))
    valid_tasks = {task.ref for task in base.tasks} | {task.ref for task in candidate.tasks}
    valid_phases = {phase.ref for phase in base.phases} | {phase.ref for phase in candidate.phases}
    if (
        not set(assessment.affected_task_refs) <= valid_tasks
        or not set(assessment.affected_phase_refs) <= valid_phases
    ):
        errors.append(DraftFinding("change_assessment", "UNKNOWN_AFFECTED_REFERENCE"))
    old_tasks = {task.ref: task for task in base.tasks}
    new_tasks = {task.ref: task for task in candidate.tasks}
    for ref in old_tasks.keys() | new_tasks.keys():
        if old_tasks.get(ref) != new_tasks.get(ref) and ref not in assessment.affected_task_refs:
            errors.append(DraftFinding(f"tasks.{ref}", "UNREQUESTED_TASK_CHANGE"))
        old_task, new_task = old_tasks.get(ref), new_tasks.get(ref)
        if old_task is not None and old_task.duration_source in {
            "USER_STATED",
            "AUTHORIZED_SOURCE",
        }:
            changed = new_task is None or (
                new_task.duration_days != old_task.duration_days
                or new_task.duration_source != old_task.duration_source
                or new_task.duration_source_refs != old_task.duration_source_refs
            )
            explicit_duration = f"tasks.{ref}.duration_days" in assessment.affected_field_paths
            if changed and (
                not explicit_duration
                or not assessment.source_refs
                or new_task is None
                or new_task.duration_source != "USER_STATED"
                or not set(new_task.duration_source_refs) & set(assessment.source_refs)
            ):
                errors.append(
                    DraftFinding(f"tasks.{ref}.duration_days", "USER_TASK_DURATION_CHANGED")
                )
    old_phases = {phase.ref: phase for phase in base.phases}
    new_phases = {phase.ref: phase for phase in candidate.phases}
    for ref in old_phases.keys() | new_phases.keys():
        if old_phases.get(ref) != new_phases.get(ref) and ref not in assessment.affected_phase_refs:
            errors.append(DraftFinding(f"phases.{ref}", "UNREQUESTED_PHASE_CHANGE"))
    removed = set(base.dependencies) - set(candidate.dependencies)
    if any(
        edge.predecessor_ref not in assessment.affected_task_refs
        or edge.successor_ref not in assessment.affected_task_refs
        for edge in removed
    ):
        errors.append(DraftFinding("dependencies", "UNREQUESTED_DEPENDENCY_CHANGE"))
    return DraftValidationResult(errors=tuple(errors))


def planning_source_references(draft: PlanningDraft) -> frozenset[str]:
    refs: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for key, child in cast(dict[str, object], value).items():
                if key in {"source_refs", "duration_source_refs"} and isinstance(child, list):
                    refs.update(str(ref) for ref in cast(list[object], child))
                else:
                    collect(child)
        elif isinstance(value, list):
            for child in cast(list[object], value):
                collect(child)

    collect(draft.model_dump(mode="json"))
    return frozenset(refs)


def planning_hard_constraints(draft: PlanningDraft) -> dict[str, object]:
    return {
        path: field
        for path, field in _fields(
            draft.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
        ).items()
        if field.get("status") in {"USER_STATED", "AUTHORIZED_SOURCE"}
    }
