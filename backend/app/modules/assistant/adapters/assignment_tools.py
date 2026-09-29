"""Application-service-only dispatch for Phase 3 Assignment Tools."""

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, Protocol, cast
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.application.member_service import MemberForbiddenError, MemberService
from app.modules.people_capacity.application.service import PeopleCapacityService
from app.modules.work.application.project_service import ProjectService
from app.modules.work.application.task_service import TaskService
from app.modules.work.planning.application.manual_service import ManualPlanningService
from app.modules.work.planning.assignment.application.assignment_service import (
    ExplicitAssignmentCommand,
    ExplicitTaskAssignmentService,
)
from app.modules.work.planning.assignment.application.recommendation_service import (
    CreateRecommendationCommand,
    ReviseRecommendationCommand,
    TeamRecommendationService,
)
from app.modules.work.planning.assignment.application.requirement_service import (
    ConfirmRequirementsCommand,
    DeriveRequirementsCommand,
    GetRequirementsQuery,
    RequirementSet,
    TeamRequirementNotFoundError,
    TeamRequirementReferenceError,
    TeamRequirementService,
    TeamRequirementStatus,
)
from app.modules.work.planning.assignment.domain.ranking_preview import CandidateRankingPreview
from app.modules.work.planning.assignment.domain.recommendations import (
    CandidateOverride,
    RecommendationVersion,
)
from app.modules.work.planning.domain.project_weeks import ProjectWeek
from work_management_ai.agents.assignment.contracts import (
    CandidateSnapshot,
    EvidenceSnapshot,
    ExplicitAssignmentSnapshot,
    ProjectWorkloadSnapshot,
    ScoreBreakdown,
    SelectedMemberSnapshot,
    TeamRecommendationSnapshot,
    TeamRequirementsPendingSnapshot,
    WorkloadSnapshot,
)
from work_management_ai.agents.orchestrator.contracts import (
    ActiveTeamContext,
    ExactAssignmentContext,
    ExactAssignmentResolution,
)
from work_management_ai.runtime.contracts import (
    ActorReference,
    ToolExecutionRequest,
    ToolExecutionResult,
)
from work_management_ai.tools.assignment.assign_task.adapter import AssignTaskToolAdapter
from work_management_ai.tools.assignment.assign_task.contracts import (
    AssignTaskApplicationPort,
    AssignTaskInput,
)
from work_management_ai.tools.assignment.manage_team.adapter import ManageTeamToolAdapter
from work_management_ai.tools.assignment.manage_team.contracts import (
    ManageTeamApplicationPort,
    ManageTeamInput,
)
from work_management_ai.tools.assignment.read_workload.adapter import ReadWorkloadToolAdapter
from work_management_ai.tools.assignment.read_workload.contracts import (
    ReadWorkloadApplicationPort,
    ReadWorkloadInput,
)


def _normalize_reference(value: str) -> str:
    normalized = value.strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {'"', "'"}:
        return normalized[1:-1].strip()
    return normalized


class AssignmentApplicationPort(
    ManageTeamApplicationPort,
    ReadWorkloadApplicationPort,
    AssignTaskApplicationPort,
    Protocol,
):
    """Combined narrow boundary implemented by backend application services."""


class AssistantAssignmentToolAdapter:
    """Route only declared Assignment tools; no repository access is permitted."""

    def __init__(self, *, application: AssignmentApplicationPort) -> None:
        self._adapters = {
            "assignment.manage_team": ManageTeamToolAdapter(application=application),
            "assignment.read_workload": ReadWorkloadToolAdapter(application=application),
            "assignment.assign_task": AssignTaskToolAdapter(application=application),
        }

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        adapter = self._adapters.get(request.tool_id)
        if adapter is None:
            return ToolExecutionResult(
                status="REJECTED", typed_output={}, safe_error_code="TOOL_NOT_ALLOWED"
            )
        return await adapter.execute(request)


class CurrentActorResolverPort(Protocol):
    async def resolve(
        self, *, organization_id: UUID, membership_id: UUID
    ) -> AuthenticatedActor | None: ...


class AssignmentApplicationService:
    """Translate AI Tool contracts into existing deterministic use cases."""

    _REVISION_PATTERNS = (
        re.compile(
            r"^\s*replace\s+(?P<old>.+?)\s+with\s+(?P<new>.+?)"
            r"(?:\s+because\s+(?P<reason>.+))?\s*$",
            re.I,
        ),
        re.compile(
            r"^\s*thay\s+(?P<old>.+?)\s+bằng\s+(?P<new>.+?)"
            r"(?:\s+vì\s+(?P<reason>.+))?\s*$",
            re.I,
        ),
    )

    def __init__(
        self,
        *,
        actor_resolver: CurrentActorResolverPort,
        requirements: TeamRequirementService,
        recommendations: TeamRecommendationService,
        assignments: ExplicitTaskAssignmentService,
        people_capacity: PeopleCapacityService,
        planning: ManualPlanningService,
        members: MemberService,
    ) -> None:
        self._actors = actor_resolver
        self._requirements = requirements
        self._recommendations = recommendations
        self._assignments = assignments
        self._people_capacity = people_capacity
        self._planning = planning
        self._members = members

    async def _actor(self, reference: ActorReference) -> AuthenticatedActor:
        actor = await self._actors.resolve(
            organization_id=reference.organization_id,
            membership_id=reference.membership_id,
        )
        if actor is None or (
            actor.organization_id != reference.organization_id
            or actor.membership_id != reference.membership_id
        ):
            raise ValueError("ACTOR_CONTEXT_UNAVAILABLE")
        return actor

    async def manage_team(
        self,
        *,
        actor: ActorReference,
        value: ManageTeamInput,
        idempotency_key: str,
    ) -> TeamRecommendationSnapshot | TeamRequirementsPendingSnapshot:
        current = await self._actor(actor)
        if value.action == "REVISE":
            return await self._revise_team(
                actor=current,
                value=value,
                idempotency_key=idempotency_key,
            )
        if value.project_id is None:
            raise ValueError("PROJECT_CONTEXT_REQUIRED")
        derived_from_exact_planning = False
        derive_command = DeriveRequirementsCommand(
            actor=current,
            project_id=value.project_id,
            request_id=f"assistant:{idempotency_key}:derive",
            idempotency_key=f"{idempotency_key}:derive-requirements",
        )
        try:
            requirements = await self._requirements.get_for_project(
                GetRequirementsQuery(actor=current, project_id=value.project_id)
            )
        except TeamRequirementNotFoundError:
            requirements = await self._requirements.derive(derive_command)
            derived_from_exact_planning = True
        else:
            if (
                requirements.status is not TeamRequirementStatus.CONFIRMED
                and value.planning_proposal_id is not None
                and value.planning_proposal_version is not None
            ):
                try:
                    replayed = await self._requirements.derive(derive_command)
                except TeamRequirementReferenceError:
                    pass
                else:
                    derived_from_exact_planning = (
                        replayed.id == requirements.id and replayed.version == requirements.version
                    )
        if requirements.incomplete_items:
            return self._requirements_pending_snapshot(
                actor=current,
                project_id=value.project_id,
                requirements=requirements,
                reason_codes=tuple(sorted({item.reason for item in requirements.incomplete_items})),
            )
        if requirements.status is not TeamRequirementStatus.CONFIRMED:
            if (
                not derived_from_exact_planning
                or value.planning_proposal_id is None
                or value.planning_proposal_version is None
            ):
                return self._requirements_pending_snapshot(
                    actor=current,
                    project_id=value.project_id,
                    requirements=requirements,
                    reason_codes=("MANAGER_CONFIRMATION_REQUIRED",),
                )
            requirements = await self._requirements.confirm(
                ConfirmRequirementsCommand(
                    actor=current,
                    project_id=value.project_id,
                    requirement_set_id=requirements.id,
                    expected_version=requirements.version,
                    request_id=f"assistant:{idempotency_key}:confirm",
                    idempotency_key=f"{idempotency_key}:confirm-requirements",
                )
            )
        version = await self._recommendations.create(
            CreateRecommendationCommand(
                actor=current,
                project_id=value.project_id,
                requirement_set_id=requirements.id,
                requirement_version=requirements.version,
                request_id=f"assistant:{idempotency_key}",
                idempotency_key=idempotency_key,
            )
        )
        return self._recommendation_snapshot(
            organization_id=current.organization_id,
            project_id=value.project_id,
            version=version,
        )

    @staticmethod
    def _requirements_pending_snapshot(
        *,
        actor: AuthenticatedActor,
        project_id: UUID,
        requirements: RequirementSet,
        reason_codes: tuple[str, ...],
    ) -> TeamRequirementsPendingSnapshot:
        return TeamRequirementsPendingSnapshot(
            organization_id=actor.organization_id,
            project_id=project_id,
            requirement_set_id=requirements.id,
            requirement_version=requirements.version,
            reason_codes=reason_codes,
            observed_at=datetime.now(UTC),
        )

    async def _revise_team(
        self,
        *,
        actor: AuthenticatedActor,
        value: ManageTeamInput,
        idempotency_key: str,
    ) -> TeamRecommendationSnapshot:
        if (
            value.project_id is None
            or value.recommendation_id is None
            or value.recommendation_version is None
            or value.revision_instruction is None
        ):
            raise ValueError("TEAM_REVISION_CONTEXT_REQUIRED")
        match = next(
            (
                pattern.match(value.revision_instruction)
                for pattern in self._REVISION_PATTERNS
                if pattern.match(value.revision_instruction)
            ),
            None,
        )
        if match is None:
            raise ValueError("TEAM_REVISION_CLARIFICATION_REQUIRED")
        old_name = _normalize_reference(match.group("old"))
        new_name = _normalize_reference(match.group("new"))
        reason = (match.group("reason") or "").strip()
        if not reason:
            raise ValueError("TEAM_REVISION_REASON_REQUIRED")

        current = await self._recommendations.get(
            actor,
            value.recommendation_id,
            value.recommendation_version,
        )
        selected_ids = {item.membership_id for item in current.selections}
        old_ids = {
            item.membership_id
            for item in current.alternatives
            if item.membership_id in selected_ids
            and item.display_name.casefold() == old_name.casefold()
        }
        people = await self._members.list_members(
            actor=actor,
            query=new_name,
            role=None,
            is_active=True,
            page=1,
            page_size=2,
        )
        exact_people = tuple(
            item for item in people.items if item.display_name.casefold() == new_name.casefold()
        )
        if len(old_ids) != 1 or len(exact_people) != 1:
            raise ValueError("TEAM_REVISION_CLARIFICATION_REQUIRED")
        old_id = next(iter(old_ids))
        new_id = exact_people[0].membership_id
        eligible_pairs = {
            (item.requirement_id, item.membership_id)
            for item in current.alternatives
            if item.eligible and not item.hard_failure_codes
        }
        overrides = tuple(
            CandidateOverride(
                requirement_id=item.requirement_id,
                selected_membership_id=(
                    new_id if item.membership_id == old_id else item.membership_id
                ),
                override_reason=(reason if item.membership_id == old_id else item.override_reason),
                allocated_effort_hours=item.allocated_effort_hours,
            )
            for item in current.selections
        )
        replaced = tuple(item for item in overrides if item.selected_membership_id == new_id)
        if not replaced or any(
            (item.requirement_id, item.selected_membership_id) not in eligible_pairs
            for item in replaced
        ):
            raise ValueError("TEAM_REVISION_CANDIDATE_INELIGIBLE")
        revised = await self._recommendations.revise(
            ReviseRecommendationCommand(
                actor=actor,
                recommendation_id=value.recommendation_id,
                expected_version=value.recommendation_version,
                overrides=overrides,
                request_id=f"assistant:{idempotency_key}",
                idempotency_key=idempotency_key,
            )
        )
        return self._recommendation_snapshot(
            organization_id=actor.organization_id,
            project_id=value.project_id,
            version=revised,
        )

    async def read_workload(
        self, *, actor: ActorReference, value: ReadWorkloadInput
    ) -> ProjectWorkloadSnapshot:
        current = await self._actor(actor)
        weeks = await self._planning.list_project_weeks(
            actor=current, project_id=value.project_id, page=1, page_size=100
        )
        members = await self._members.list_members(
            actor=current, query=None, role=None, is_active=True, page=1, page_size=100
        )
        workloads: list[WorkloadSnapshot] = []
        for raw_week in weeks.items:
            if not isinstance(raw_week, ProjectWeek):
                continue
            week = raw_week
            for member in members.items:
                values = await self._people_capacity.list_weekly_workload(
                    actor=current,
                    week_start=week.start_date,
                    membership_id=member.membership_id,
                )
                for item in values:
                    if item.project_week_id == week.id:
                        workloads.append(
                            WorkloadSnapshot(
                                membership_id=item.membership_id,
                                project_week_id=item.project_week_id,
                                effective_capacity_hours=item.effective_capacity_hours,
                                allocated_effort_hours=item.allocated_effort_hours,
                                residual_capacity_hours=item.residual_capacity_hours,
                                workload_ratio=item.workload_ratio,
                            )
                        )
        return ProjectWorkloadSnapshot(
            organization_id=current.organization_id,
            project_id=value.project_id,
            workloads=tuple(workloads),
            observed_at=datetime.now(UTC),
        )

    async def assign_task(
        self,
        *,
        actor: ActorReference,
        value: AssignTaskInput,
        idempotency_key: str,
    ) -> ExplicitAssignmentSnapshot:
        current = await self._actor(actor)
        result = await self._assignments.assign(
            ExplicitAssignmentCommand(
                actor=current,
                task_id=value.task_id,
                assignee_membership_id=value.membership_id,
                expected_task_version=value.task_version,
                request_id=f"assistant:{idempotency_key}",
                idempotency_key=idempotency_key,
            )
        )
        return ExplicitAssignmentSnapshot(
            organization_id=current.organization_id,
            project_id=result.task.project_id,
            task_id=result.task.id,
            task_version=result.task.version,
            membership_id=value.membership_id,
            warning_codes=tuple(item.code for item in result.warnings),
            effective_capacity_hours=result.effective_capacity_hours,
            workload_before_hours=result.workload_before_hours,
            workload_after_hours=result.workload_after_hours,
            observed_at=datetime.now(UTC),
        )

    @staticmethod
    def _recommendation_snapshot(
        *, organization_id: UUID, project_id: UUID, version: RecommendationVersion
    ) -> TeamRecommendationSnapshot:
        candidates = {
            (item.requirement_id, item.membership_id): item for item in version.alternatives
        }

        def score(item: CandidateRankingPreview) -> ScoreBreakdown:
            return ScoreBreakdown(
                skill_points=item.skill_points,
                capacity_points=item.capacity_points,
                evidence_points=item.evidence_points,
                familiarity_points=item.familiarity_points,
                total_points=item.total_points,
            )

        def evidence(item: CandidateRankingPreview) -> tuple[EvidenceSnapshot, ...]:
            return tuple(
                EvidenceSnapshot(
                    evidence_id=str(value.id),
                    summary=value.summary,
                    source_resource_type=value.source_resource_type,
                    source_resource_id=value.source_resource_id,
                )
                for value in item.evidence
            )

        selected: list[SelectedMemberSnapshot] = []
        for item in version.selections:
            candidate = candidates[(item.requirement_id, item.membership_id)]
            workload_ratio = None
            if candidate.effective_capacity_hours:
                workload_ratio = (
                    Decimal(candidate.effective_capacity_hours)
                    - Decimal(candidate.residual_capacity_hours or 0)
                ) / Decimal(candidate.effective_capacity_hours)
            selected.append(
                SelectedMemberSnapshot(
                    membership_id=item.membership_id,
                    display_name=candidate.display_name,
                    requirement_ids=(item.requirement_id,),
                    scores=score(candidate),
                    evidence=evidence(candidate),
                    workload_ratio=workload_ratio,
                    warning_codes=item.warning_codes,
                )
            )
        alternatives = tuple(
            CandidateSnapshot(
                membership_id=item.membership_id,
                display_name=item.display_name,
                requirement_id=item.requirement_id,
                eligible=item.eligible,
                hard_failure_codes=item.hard_failure_codes,
                scores=score(item),
                evidence=evidence(item),
                workload_ratio=None,
            )
            for item in version.alternatives
        )
        return TeamRecommendationSnapshot(
            organization_id=organization_id,
            project_id=project_id,
            requirement_set_id=version.requirement_set_id,
            requirement_version=version.requirement_version,
            recommendation_id=version.recommendation_id,
            version=version.version,
            status=version.status,
            policy_version=version.policy_version,
            selected_members=tuple(selected),
            alternatives=alternatives,
            uncovered_requirement_ids=tuple(item.requirement_id for item in version.uncovered),
            observed_at=datetime.now(UTC),
        )


class AssistantAssignmentContextResolver:
    """Resolve an explicit Task/person command without granting write authority."""

    _PATTERNS = (
        re.compile(r"^\s*assign\s+(?:task\s+)?(?P<task>.+?)\s+to\s+(?P<member>.+?)\s*$", re.I),
        re.compile(
            r"^\s*giao\s+(?:(?:task|công việc)\s+)?(?P<task>.+?)\s+cho\s+(?P<member>.+?)\s*$",
            re.I,
        ),
    )

    _PROJECT_PATTERNS: tuple[
        tuple[Literal["RECOMMEND_TEAM", "ANALYZE_WORKLOAD"], re.Pattern[str]], ...
    ] = (
        (
            "RECOMMEND_TEAM",
            re.compile(
                r"^\s*(?:recommend|create|form)\s+(?:a\s+)?team\s+for\s+"
                r"(?:project\s+)?(?P<project>.+?)\s*$",
                re.I,
            ),
        ),
        (
            "ANALYZE_WORKLOAD",
            re.compile(
                r"^\s*analy[sz]e\s+(?:the\s+)?workload\s+(?:for|of)\s+"
                r"(?:project\s+)?(?P<project>.+?)\s*$",
                re.I,
            ),
        ),
        (
            "RECOMMEND_TEAM",
            re.compile(
                r"^\s*(?:đề xuất|tạo|lập)\s+(?:một\s+)?(?:đội|nhóm)\s+"
                r"(?:cho|của)\s+(?:dự án\s+)?(?P<project>.+?)\s*$",
                re.I,
            ),
        ),
        (
            "ANALYZE_WORKLOAD",
            re.compile(
                r"^\s*phân tích\s+(?:khối lượng|tải)\s+(?:cho|của)\s+"
                r"(?:dự án\s+)?(?P<project>.+?)\s*$",
                re.I,
            ),
        ),
    )

    def __init__(
        self,
        *,
        tasks: TaskService,
        members: MemberService,
        projects: ProjectService | None = None,
    ) -> None:
        self._tasks = tasks
        self._members = members
        self._projects = projects

    @staticmethod
    def _reference(value: str) -> str:
        return _normalize_reference(value)

    async def resolve_exact_assignment(
        self, *, actor: AuthenticatedActor, message: str
    ) -> ExactAssignmentContext | None:
        return (await self.resolve_assignment(actor=actor, message=message)).exact_context

    async def resolve_assignment(
        self, *, actor: AuthenticatedActor, message: str
    ) -> ExactAssignmentResolution:
        match = next(
            (pattern.match(message) for pattern in self._PATTERNS if pattern.match(message)),
            None,
        )
        if match is None:
            project_match = next(
                (
                    (operation, pattern.match(message))
                    for operation, pattern in self._PROJECT_PATTERNS
                    if pattern.match(message)
                ),
                None,
            )
            if project_match is None:
                return ExactAssignmentResolution()
            if self._projects is None:
                return ExactAssignmentResolution(issue="PROJECT_AMBIGUOUS_OR_NOT_FOUND")
            operation, matched = project_match
            assert matched is not None
            project_name = _normalize_reference(matched.group("project"))
            projects = await self._projects.list_projects(
                actor=actor,
                query=project_name,
                page=1,
                page_size=2,
            )
            exact_projects = tuple(
                item for item in projects.items if item.name.casefold() == project_name.casefold()
            )
            if len(exact_projects) != 1:
                return ExactAssignmentResolution(issue="PROJECT_AMBIGUOUS_OR_NOT_FOUND")
            return ExactAssignmentResolution(
                team_context=ActiveTeamContext(
                    project_id=exact_projects[0].id,
                    requested_operation=cast(
                        Literal["RECOMMEND_TEAM", "ANALYZE_WORKLOAD"], operation
                    ),
                )
            )
        task_reference = _normalize_reference(match.group("task"))
        member_reference = _normalize_reference(match.group("member"))
        tasks = await self._tasks.find_visible_tasks_by_title(
            actor=actor, query=task_reference, limit=20
        )
        exact_tasks = tuple(
            item for item in tasks if item.title.casefold() == task_reference.casefold()
        )
        try:
            people = await self._members.list_members(
                actor=actor,
                query=member_reference,
                role=None,
                is_active=True,
                page=1,
                page_size=100,
            )
        except MemberForbiddenError:
            return ExactAssignmentResolution(issue="ASSIGNMENT_FORBIDDEN")
        exact_people = tuple(
            item
            for item in people.items
            if item.display_name.casefold() == member_reference.casefold()
        )
        if len(exact_tasks) != 1:
            return ExactAssignmentResolution(issue="TASK_AMBIGUOUS_OR_NOT_FOUND")
        if len(exact_people) != 1:
            return ExactAssignmentResolution(issue="MEMBER_AMBIGUOUS_OR_NOT_FOUND")
        task, member = exact_tasks[0], exact_people[0]
        return ExactAssignmentResolution(
            exact_context=ExactAssignmentContext(
                project_id=task.project_id,
                task_id=task.id,
                task_version=task.version,
                membership_id=member.membership_id,
            )
        )


class TeamRecommendationSnapshotAdapter:
    """Hydrate current recommendation version through its application service."""

    def __init__(self, service: TeamRecommendationService) -> None:
        self._service = service

    async def get_recommendation_version(
        self, *, actor: AuthenticatedActor, recommendation_id: UUID
    ) -> int | None:
        try:
            value = await self._service.get(actor, recommendation_id)
        except Exception:
            return None
        return value.version
