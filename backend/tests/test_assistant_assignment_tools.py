"""Phase 3 Assistant-to-Assignment Tool bridge tests."""

# pyright: reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownArgumentType=false, reportArgumentType=false, reportUnknownMemberType=false

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.modules.assistant.adapters.assignment_tools import (
    AssignmentApplicationService,
    AssistantAssignmentContextResolver,
    AssistantAssignmentToolAdapter,
)
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.application.member_service import (
    MemberForbiddenError,
    MemberPage,
    MemberSummary,
)
from app.modules.organization.domain.roles import MembershipRole
from app.modules.work.planning.assignment.application.recommendation_service import (
    ReviseRecommendationCommand,
)
from app.modules.work.planning.assignment.application.requirement_service import (
    TeamRequirementStatus,
)
from app.modules.work.planning.assignment.domain.ranking_preview import CandidateRankingPreview
from app.modules.work.planning.assignment.domain.recommendations import (
    RecommendationSelection,
    RecommendationVersion,
)
from work_management_ai.agents.assignment.contracts import (
    ExplicitAssignmentSnapshot,
    ProjectWorkloadSnapshot,
    TeamRecommendationSnapshot,
    TeamRequirementsPendingSnapshot,
)
from work_management_ai.runtime.contracts import ActorReference, ToolExecutionRequest
from work_management_ai.tools.assignment.manage_team.contracts import ManageTeamInput


class _Application:
    def __init__(self, organization_id: UUID) -> None:
        self.organization_id = organization_id
        self.calls: list[tuple[str, object]] = []

    async def manage_team(self, *, actor, value, idempotency_key):
        self.calls.append(("manage_team", value))
        return TeamRecommendationSnapshot(
            organization_id=self.organization_id,
            project_id=value.project_id,
            requirement_set_id=uuid4(),
            requirement_version=1,
            recommendation_id=uuid4(),
            version=1,
            status="PROPOSED",
            policy_version="ranking-v1",
            selected_members=(),
            alternatives=(),
            uncovered_requirement_ids=(),
            observed_at=datetime.now(UTC),
        )

    async def read_workload(self, *, actor, value):
        self.calls.append(("read_workload", value))
        return ProjectWorkloadSnapshot(
            organization_id=self.organization_id,
            project_id=value.project_id,
            workloads=(),
            observed_at=datetime.now(UTC),
        )

    async def assign_task(self, *, actor, value, idempotency_key):
        self.calls.append(("assign_task", value))
        return ExplicitAssignmentSnapshot(
            organization_id=self.organization_id,
            project_id=uuid4(),
            task_id=value.task_id,
            task_version=value.task_version + 1,
            membership_id=value.membership_id,
            effective_capacity_hours=40,
            workload_before_hours=8,
            workload_after_hours=16,
            observed_at=datetime.now(UTC),
        )


def _request(tool_id: str, actor: ActorReference, typed_input: dict[str, object]):
    return ToolExecutionRequest(
        agent_run_id=uuid4(),
        tool_id=tool_id,
        tool_version="1.0.0",
        call_id=f"{tool_id}:1",
        actor=actor,
        typed_input=typed_input,
        idempotency_key=f"idem:{tool_id}",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_id", "payload", "expected_call"),
    [
        ("assignment.manage_team", {"action": "CREATE", "project_id": None}, "manage_team"),
        ("assignment.read_workload", {"project_id": None}, "read_workload"),
        (
            "assignment.assign_task",
            {"task_id": None, "task_version": 2, "membership_id": None},
            "assign_task",
        ),
    ],
)
async def test_assignment_bridge_routes_only_typed_application_ports(
    tool_id: str, payload: dict[str, object], expected_call: str
) -> None:
    organization_id = uuid4()
    actor = ActorReference(membership_id=uuid4(), organization_id=organization_id)
    payload = {key: (str(uuid4()) if value is None else value) for key, value in payload.items()}
    application = _Application(organization_id)
    adapter = AssistantAssignmentToolAdapter(application=application)

    result = await adapter.execute(_request(tool_id, actor, payload))

    assert result.status == "SUCCEEDED"
    assert [name for name, _ in application.calls] == [expected_call]
    assert result.evidence[0].organization_id == organization_id


@pytest.mark.asyncio
async def test_assignment_bridge_rejects_unknown_tool_without_application_call() -> None:
    organization_id = uuid4()
    actor = ActorReference(membership_id=uuid4(), organization_id=organization_id)
    application = _Application(organization_id)
    adapter = AssistantAssignmentToolAdapter(application=application)

    result = await adapter.execute(_request("assignment.unknown", actor, {}))

    assert result.status == "REJECTED"
    assert result.safe_error_code == "TOOL_NOT_ALLOWED"
    assert application.calls == []


@pytest.mark.asyncio
async def test_explicit_assignment_resolver_normalizes_quoted_exact_references() -> None:
    actor = AuthenticatedActor(
        user_id=uuid4(),
        email="manager@example.test",
        display_name="Manager",
        membership_id=uuid4(),
        organization_id=uuid4(),
        organization_name="Tenant",
        role=MembershipRole.MANAGER,
    )
    task = SimpleNamespace(id=uuid4(), project_id=uuid4(), version=4, title="Launch brief")
    member = MemberSummary(uuid4(), "Lan", MembershipRole.EMPLOYEE, True)

    class Tasks:
        async def find_visible_tasks_by_title(self, *, query, **_):
            return (task,) if query == "Launch brief" else ()

    class Members:
        async def list_members(self, *, query, **_):
            items = (member,) if query == "Lan" else ()
            return MemberPage(items=items, page=1, page_size=2, total=len(items))

    resolver = AssistantAssignmentContextResolver(tasks=Tasks(), members=Members())

    result = await resolver.resolve_exact_assignment(
        actor=actor,
        message='Assign task "Launch brief" to "Lan"',
    )

    assert result is not None
    assert result.task_id == task.id
    assert result.task_version == 4
    assert result.membership_id == member.membership_id


@pytest.mark.asyncio
async def test_explicit_assignment_resolver_returns_clarification_for_ambiguous_task() -> None:
    actor = AuthenticatedActor(
        user_id=uuid4(),
        email="manager@example.test",
        display_name="Manager",
        membership_id=uuid4(),
        organization_id=uuid4(),
        organization_name="Tenant",
        role=MembershipRole.MANAGER,
    )
    tasks = (SimpleNamespace(id=uuid4(), project_id=uuid4(), version=1, title="Task A"),) * 2
    member = MemberSummary(uuid4(), "Lan", MembershipRole.EMPLOYEE, True)

    class Tasks:
        async def find_visible_tasks_by_title(self, **_):
            return tasks

    class Members:
        async def list_members(self, **_):
            return MemberPage(items=(member,), page=1, page_size=2, total=1)

    resolver = AssistantAssignmentContextResolver(tasks=Tasks(), members=Members())

    result = await resolver.resolve_assignment(
        actor=actor,
        message="Assign Task A to Lan",
    )

    assert result.exact_context is None
    assert result.issue == "TASK_AMBIGUOUS_OR_NOT_FOUND"


@pytest.mark.asyncio
async def test_explicit_assignment_resolver_rejects_unique_substring_matches() -> None:
    actor = AuthenticatedActor(
        user_id=uuid4(),
        email="manager@example.test",
        display_name="Manager",
        membership_id=uuid4(),
        organization_id=uuid4(),
        organization_name="Tenant",
        role=MembershipRole.MANAGER,
    )
    task = SimpleNamespace(id=uuid4(), project_id=uuid4(), version=1, title="Task A follow-up")
    member = MemberSummary(uuid4(), "Lan Anh", MembershipRole.EMPLOYEE, True)

    class Tasks:
        async def find_visible_tasks_by_title(self, **_):
            return (task,)

    class Members:
        async def list_members(self, **_):
            return MemberPage(items=(member,), page=1, page_size=100, total=1)

    result = await AssistantAssignmentContextResolver(
        tasks=Tasks(), members=Members()
    ).resolve_assignment(actor=actor, message="Assign Task A to Lan")

    assert result.exact_context is None
    assert result.issue == "TASK_AMBIGUOUS_OR_NOT_FOUND"


@pytest.mark.asyncio
async def test_employee_explicit_assignment_resolution_returns_deterministic_denial() -> None:
    actor = AuthenticatedActor(
        user_id=uuid4(),
        email="employee@example.test",
        display_name="Employee",
        membership_id=uuid4(),
        organization_id=uuid4(),
        organization_name="Tenant",
        role=MembershipRole.EMPLOYEE,
    )
    task = SimpleNamespace(id=uuid4(), project_id=uuid4(), version=1, title="Task A")

    class Tasks:
        async def find_visible_tasks_by_title(self, **_):
            return (task,)

    class Members:
        async def list_members(self, **_):
            raise MemberForbiddenError

    result = await AssistantAssignmentContextResolver(
        tasks=Tasks(), members=Members()
    ).resolve_assignment(actor=actor, message="Assign Task A to Lan")

    assert result.exact_context is None
    assert result.issue == "ASSIGNMENT_FORBIDDEN"


@pytest.mark.asyncio
async def test_team_request_resolves_one_tenant_visible_project() -> None:
    actor = AuthenticatedActor(
        user_id=uuid4(),
        email="manager@example.test",
        display_name="Manager",
        membership_id=uuid4(),
        organization_id=uuid4(),
        organization_name="Tenant",
        role=MembershipRole.MANAGER,
    )
    project = SimpleNamespace(id=uuid4(), name="Atlas")

    class Projects:
        async def list_projects(self, *, query, **_):
            assert query == "Atlas"
            return SimpleNamespace(items=(project,))

    resolver = AssistantAssignmentContextResolver(
        tasks=None,
        members=None,
        projects=Projects(),
    )

    result = await resolver.resolve_assignment(
        actor=actor,
        message='Recommend a team for Project "Atlas"',
    )

    assert result.issue is None
    assert result.team_context is not None
    assert result.team_context.project_id == project.id
    assert result.team_context.requested_operation == "RECOMMEND_TEAM"


@pytest.mark.asyncio
async def test_team_revision_replaces_only_exact_named_candidate_at_expected_version() -> None:
    actor = AuthenticatedActor(
        user_id=uuid4(),
        email="manager@example.test",
        display_name="Manager",
        membership_id=uuid4(),
        organization_id=uuid4(),
        organization_name="Tenant",
        role=MembershipRole.MANAGER,
    )
    project_id, recommendation_id, requirement_id = uuid4(), uuid4(), uuid4()
    old_member, new_member = uuid4(), uuid4()

    def candidate(member_id: UUID, name: str) -> CandidateRankingPreview:
        return CandidateRankingPreview(
            requirement_id=requirement_id,
            membership_id=member_id,
            display_name=name,
            eligible=True,
            hard_failure_codes=(),
            skill_points=Decimal("0.5"),
            capacity_points=Decimal("0.3"),
            evidence_points=Decimal("0"),
            familiarity_points=Decimal("0"),
            total_points=Decimal("0.8"),
            effective_capacity_hours=40,
            residual_capacity_hours=32,
            evidence=(),
        )

    current = RecommendationVersion(
        recommendation_id=recommendation_id,
        version=2,
        requirement_set_id=uuid4(),
        requirement_version=1,
        policy_version="ranking-v1",
        selections=(RecommendationSelection(requirement_id, old_member, Decimal("8")),),
        alternatives=(candidate(old_member, "Lan"), candidate(new_member, "Minh")),
        uncovered=(),
        demands=(),
        diff=None,
    )

    class Actors:
        async def resolve(self, **_):
            return actor

    class Recommendations:
        def __init__(self) -> None:
            self.command: ReviseRecommendationCommand | None = None

        async def get(self, resolved_actor, resolved_id, version=None):
            assert resolved_actor == actor
            assert resolved_id == recommendation_id
            assert version == 2
            return current

        async def revise(self, command):
            self.command = command
            return replace(
                current,
                version=3,
                selections=(
                    RecommendationSelection(
                        requirement_id,
                        new_member,
                        Decimal("8"),
                        override_reason="Lan quá tải",
                    ),
                ),
            )

    class Members:
        async def list_members(self, *, query, **_):
            items = (
                (MemberSummary(new_member, "Minh", MembershipRole.EMPLOYEE, True),)
                if query == "Minh"
                else ()
            )
            return MemberPage(items=items, page=1, page_size=2, total=len(items))

    recommendations = Recommendations()
    service = AssignmentApplicationService(
        actor_resolver=Actors(),
        requirements=None,
        recommendations=recommendations,
        assignments=None,
        people_capacity=None,
        planning=None,
        members=Members(),
    )

    result = await service.manage_team(
        actor=ActorReference(
            organization_id=actor.organization_id,
            membership_id=actor.membership_id,
        ),
        value=ManageTeamInput(
            action="REVISE",
            project_id=project_id,
            recommendation_id=recommendation_id,
            recommendation_version=2,
            revision_instruction="Thay Lan bằng Minh vì Lan quá tải",
        ),
        idempotency_key="revise:stable",
    )

    assert isinstance(result, TeamRecommendationSnapshot)
    assert result.project_id == project_id
    assert result.version == 3
    command = recommendations.command
    assert command is not None
    assert command.expected_version == 2
    assert command.overrides[0].selected_membership_id == new_member
    assert command.overrides[0].override_reason == "Lan quá tải"


@pytest.mark.asyncio
async def test_existing_requirement_draft_requires_manager_confirmation() -> None:
    actor = AuthenticatedActor(
        user_id=uuid4(),
        email="manager@example.test",
        display_name="Manager",
        membership_id=uuid4(),
        organization_id=uuid4(),
        organization_name="Tenant",
        role=MembershipRole.MANAGER,
    )
    project_id = uuid4()

    class Actors:
        async def resolve(self, **_):
            return actor

    class Requirements:
        confirm_calls = 0

        async def get_for_project(self, _):
            return SimpleNamespace(
                id=uuid4(),
                version=2,
                status="DRAFT",
                incomplete_items=(),
            )

        async def confirm(self, _):
            self.confirm_calls += 1
            raise AssertionError("an existing draft needs the Manager confirmation flow")

    requirements = Requirements()
    service = AssignmentApplicationService(
        actor_resolver=Actors(),
        requirements=requirements,
        recommendations=None,
        assignments=None,
        people_capacity=None,
        planning=None,
        members=None,
    )

    result = await service.manage_team(
        actor=ActorReference(
            organization_id=actor.organization_id,
            membership_id=actor.membership_id,
        ),
        value=ManageTeamInput(action="CREATE", project_id=project_id),
        idempotency_key="recommend:stable",
    )

    assert isinstance(result, TeamRequirementsPendingSnapshot)
    assert requirements.confirm_calls == 0
    assert result.kind == "team_requirements_pending"
    assert result.reason_codes == ("MANAGER_CONFIRMATION_REQUIRED",)


@pytest.mark.asyncio
async def test_exact_derive_replay_survives_crash_before_automatic_confirmation() -> None:
    actor = AuthenticatedActor(
        user_id=uuid4(),
        email="manager@example.test",
        display_name="Manager",
        membership_id=uuid4(),
        organization_id=uuid4(),
        organization_name="Tenant",
        role=MembershipRole.MANAGER,
    )
    project_id, requirement_set_id = uuid4(), uuid4()
    draft = SimpleNamespace(
        id=requirement_set_id,
        version=1,
        status=TeamRequirementStatus.DRAFT,
        incomplete_items=(),
    )

    class Actors:
        async def resolve(self, **_):
            return actor

    class Requirements:
        confirm_calls = 0

        async def get_for_project(self, _):
            return draft

        async def derive(self, _):
            return draft

        async def confirm(self, _):
            self.confirm_calls += 1
            return SimpleNamespace(
                id=draft.id,
                version=2,
                status=TeamRequirementStatus.CONFIRMED,
                incomplete_items=(),
            )

    class RecommendationReached(RuntimeError):
        pass

    class Recommendations:
        async def create(self, _):
            raise RecommendationReached

    requirements = Requirements()
    service = AssignmentApplicationService(
        actor_resolver=Actors(),
        requirements=requirements,
        recommendations=Recommendations(),
        assignments=None,
        people_capacity=None,
        planning=None,
        members=None,
    )

    with pytest.raises(RecommendationReached):
        await service.manage_team(
            actor=ActorReference(
                organization_id=actor.organization_id,
                membership_id=actor.membership_id,
            ),
            value=ManageTeamInput(
                action="CREATE",
                project_id=project_id,
                planning_proposal_id=uuid4(),
                planning_proposal_version=1,
            ),
            idempotency_key="recommend:crash-retry",
        )

    assert requirements.confirm_calls == 1
