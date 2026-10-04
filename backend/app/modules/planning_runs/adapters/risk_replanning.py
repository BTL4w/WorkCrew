"""Existing-project weekly proposals using the shared approval and worker runtime."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.planning_runs.application.approval_ports import CreatedBusinessIds
from app.modules.planning_runs.application.ports import PlanningRunTransaction
from app.modules.planning_runs.domain.models import (
    PlanningRunForbiddenError,
    PlanningRunNotFoundError,
    Proposal,
    ProposalStaleError,
    ProposalVersion,
    WorkflowCheckpoint,
    WorkflowEvent,
    WorkflowJob,
    WorkflowRun,
)
from app.modules.planning_runs.domain.risk_replanning import (
    PlanSource,
    RiskPlanBinding,
    RiskPlanMetadata,
    RiskWeeklyDraft,
    TaskSource,
    WeekSource,
    draft_content,
    validate_content,
)
from app.modules.progress.adapters.progress_models import WeeklyPlanBaselineModel
from app.modules.progress.adapters.progress_repository import capture_project_baselines
from app.modules.risk.adapters.repository import RiskRepository
from app.modules.work.adapters.database_models import ProjectModel, TaskModel
from app.modules.work.domain.tasks import TaskStatus
from app.modules.work.planning.adapters.database_models import (
    AcceptanceCriterionModel,
    MilestoneModel,
    ProjectWeekModel,
)
from app.modules.work.planning.domain.acceptance_criteria import AcceptanceCriterionDraft
from work_management_ai.agents.planning.prompts.risk_replan_v1 import RISK_REPLAN_SYSTEM_PROMPT
from work_management_ai.agents.planning.risk_replanning import WeeklyReplanModelOutput
from work_management_ai.model_gateway.contracts import (
    ModelGateway,
    ModelMessage,
    StructuredModelRequest,
)


class ActorResolver(Protocol):
    async def resolve(
        self, *, organization_id: UUID, membership_id: UUID
    ) -> AuthenticatedActor | None: ...


type TransactionFactory = Callable[[AuthenticatedActor | UUID], PlanningRunTransaction]


class RiskPlanPersistence:
    def __init__(self, session: AsyncSession, timezone: str = "UTC"):
        self.session = session
        self.timezone = timezone

    async def load(self, actor: AuthenticatedActor, binding: RiskPlanBinding) -> RiskPlanMetadata:
        risk = RiskRepository(self.session, actor, self.timezone)
        await risk.authenticate()
        task = await self.session.scalar(
            select(TaskModel).where(
                TaskModel.organization_id == actor.organization_id, TaskModel.id == binding.task_id
            )
        )
        if task is None:
            raise PlanningRunNotFoundError
        project = await self.session.scalar(
            select(ProjectModel)
            .where(
                ProjectModel.organization_id == actor.organization_id,
                ProjectModel.id == task.project_id,
            )
            .with_for_update()
        )
        if project is None:
            raise ProposalStaleError
        weeks = (
            await self.session.scalars(
                select(ProjectWeekModel)
                .where(
                    ProjectWeekModel.organization_id == actor.organization_id,
                    ProjectWeekModel.project_id == project.id,
                )
                .order_by(ProjectWeekModel.week_number)
                .limit(53)
                .with_for_update()
            )
        ).all()
        tasks = (
            await self.session.scalars(
                select(TaskModel)
                .where(
                    TaskModel.organization_id == actor.organization_id,
                    TaskModel.project_id == project.id,
                )
                .order_by(TaskModel.id)
                .limit(101)
                .with_for_update()
            )
        ).all()
        context = await risk.read_context(binding.task_id)
        if (
            context.state != "READY"
            or context.scope != "MANAGER"
            or context.score is None
            or context.risk_assessment_id != binding.risk_assessment_id
            or context.fingerprint != binding.fingerprint
            or tuple(o.id for o in context.observations) != binding.observation_ids
            or context.affected_week_ids != binding.affected_week_ids
        ):
            raise ProposalStaleError("RISK_CONTEXT_CHANGED")
        entries: list[WeekSource] = []
        for week in weeks:
            baseline = await self.session.scalar(
                select(WeeklyPlanBaselineModel)
                .where(
                    WeeklyPlanBaselineModel.organization_id == actor.organization_id,
                    WeeklyPlanBaselineModel.project_week_id == week.id,
                )
                .order_by(WeeklyPlanBaselineModel.sequence.desc())
                .limit(1)
            )
            entries.append(
                WeekSource(
                    id=week.id,
                    version=week.version,
                    week_number=week.week_number,
                    start_date=week.start_date,
                    end_date=week.end_date,
                    objective=week.objective,
                    status=str(week.status),
                    baseline_id=baseline.id if baseline else None,
                    baseline_sequence=baseline.sequence if baseline else None,
                )
            )
        task_entries: list[TaskSource] = []
        for task in tasks:
            criteria = (
                await self.session.scalars(
                    select(AcceptanceCriterionModel)
                    .where(
                        AcceptanceCriterionModel.organization_id == actor.organization_id,
                        AcceptanceCriterionModel.task_id == task.id,
                    )
                    .order_by(AcceptanceCriterionModel.position, AcceptanceCriterionModel.id)
                )
            ).all()
            milestone = (
                await self.session.get(MilestoneModel, task.milestone_id)
                if task.milestone_id
                else None
            )
            task_entries.append(
                TaskSource(
                    id=task.id,
                    version=task.version,
                    project_week_id=task.project_week_id,
                    title=task.title,
                    description=task.description,
                    due_date=task.due_date,
                    estimated_effort_hours=task.estimated_effort_hours,
                    assignee_membership_id=task.assignee_membership_id,
                    status=str(task.status),
                    milestone_target_date=milestone.target_date if milestone else None,
                    required_skill_labels=tuple(task.required_skill_labels),
                    acceptance_criteria=tuple(c.text for c in criteria),
                )
            )
        return RiskPlanMetadata(
            binding=binding,
            before=PlanSource(
                project_id=project.id,
                project_version=project.version,
                project_title=project.name,
                project_description=project.description,
                weeks=tuple(entries),
                tasks=tuple(task_entries),
            ),
        )

    async def verify(self, actor: AuthenticatedActor, metadata: RiskPlanMetadata) -> None:
        current = await self.load(actor, metadata.binding)
        if current != metadata:
            raise ProposalStaleError("PLAN_CONTEXT_CHANGED")

    async def apply(
        self,
        actor: AuthenticatedActor,
        content: dict[str, Any],
        request_id: str,
        idempotency_key: str | None,
    ) -> CreatedBusinessIds:
        validate_content(content)
        metadata = RiskPlanMetadata.model_validate(content["risk_replan"])
        await self.verify(actor, metadata)
        # Capture the original graph first if this manual project has no baseline yet.
        await capture_project_baselines(
            self.session,
            actor,
            metadata.before.project_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
        old = {str(t.id): t for t in metadata.before.tasks}
        created: list[UUID] = []
        criteria_ids: list[UUID] = []
        now = datetime.now(UTC)
        for proposed in content["tasks"]:
            ref = proposed["ref"]
            due = date.fromisoformat(proposed["due_date"]) if proposed["due_date"] else None
            if ref in old:
                source = old[ref]
                if (
                    str(source.project_week_id),
                    source.due_date,
                    source.estimated_effort_hours,
                ) == (proposed["project_week_ref"], due, proposed["estimated_effort_hours"]):
                    continue
                task = await self.session.get(TaskModel, source.id)
                if task is None:
                    raise ProposalStaleError
                task.project_week_id = UUID(proposed["project_week_ref"])
                task.due_date = due
                task.estimated_effort_hours = proposed["estimated_effort_hours"]
                task.version += 1
                task.updated_at = now
                task.updated_by_membership_id = actor.membership_id
            else:
                task_id = uuid4()
                created.append(task_id)
                self.session.add(
                    TaskModel(
                        id=task_id,
                        organization_id=actor.organization_id,
                        project_id=metadata.before.project_id,
                        project_week_id=UUID(proposed["project_week_ref"]),
                        milestone_id=None,
                        title=proposed["title"].strip(),
                        description=proposed["description"],
                        due_date=due,
                        estimated_effort_hours=proposed["estimated_effort_hours"],
                        assignee_membership_id=None,
                        status=TaskStatus.TO_DO,
                        required_skill_labels=[],
                        version=1,
                        created_by_membership_id=actor.membership_id,
                        updated_by_membership_id=actor.membership_id,
                        created_at=now,
                        updated_at=now,
                    )
                )
                seen: set[str] = set()
                for position, value in enumerate(proposed["acceptance_criteria"], 1):
                    criterion = AcceptanceCriterionDraft.create(
                        task_id=task_id, text=value, position=position
                    )
                    if criterion.text in seen:
                        raise ValueError("DUPLICATE_ACCEPTANCE_CRITERION")
                    seen.add(criterion.text)
                    criterion_id = uuid4()
                    criteria_ids.append(criterion_id)
                    self.session.add(
                        AcceptanceCriterionModel(
                            id=criterion_id,
                            organization_id=actor.organization_id,
                            task_id=task_id,
                            text=criterion.text,
                            position=position,
                            version=1,
                            created_by_membership_id=actor.membership_id,
                            updated_by_membership_id=actor.membership_id,
                        )
                    )
        await self.session.flush()
        await capture_project_baselines(
            self.session,
            actor,
            metadata.before.project_id,
            kind="APPROVED",
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
        return CreatedBusinessIds(
            project_id=metadata.before.project_id,
            task_ids=tuple(created),
            acceptance_criterion_ids=tuple(criteria_ids),
        )


class RiskReplanningJobHandler:
    def __init__(
        self,
        *,
        transaction_factory: TransactionFactory,
        actor_resolver: ActorResolver,
        gateway_factory: Callable[[WorkflowRun], ModelGateway],
    ):
        self.transactions = transaction_factory
        self.actors = actor_resolver
        self.gateways = gateway_factory

    async def __call__(self, *, job: WorkflowJob, worker_id: str) -> None:
        del worker_id
        try:
            async with asyncio.timeout(120):
                await self.generate(job)
        except Exception:
            async with self.transactions(job.organization_id) as tx:
                run = await tx.repository.get_workflow_run_by_scope(
                    organization_id=job.organization_id, run_id=job.workflow_run_id
                )
                failed = run is not None and await tx.repository.fail_risk_revision_run(
                    organization_id=job.organization_id, run_id=run.id
                )
                if run is not None and (failed or job.job_type == "proposal.risk_replan_revision"):
                    await tx.repository.append_event(
                        event=WorkflowEvent(
                            id=uuid4(),
                            organization_id=job.organization_id,
                            workflow_run_id=run.id,
                            sequence=0,
                            event_type="workflow.failed" if failed else "proposal.revision_failed",
                            public_payload={
                                "safe_error_code": "RISK_REPLAN_UNAVAILABLE",
                                "manual_fallback": "PROJECT_TASK_EDITOR",
                            },
                        )
                    )
                await tx.commit()
            raise

    async def generate(self, job: WorkflowJob) -> None:
        metadata = RiskPlanMetadata.model_validate(job.payload["risk_replan"])
        actor = await self.actors.resolve(
            organization_id=job.organization_id,
            membership_id=UUID(str(job.payload["requester_membership_id"])),
        )
        if actor is None or actor.role not in {MembershipRole.MANAGER, MembershipRole.ADMIN}:
            raise PlanningRunForbiddenError
        model_context: dict[str, object] = {}
        preflight = None
        revision = job.job_type == "proposal.risk_replan_revision"
        async with self.transactions(actor) as tx:
            await tx.repository.authenticate_risk_plan_actor(actor=actor)
            run = await tx.repository.get_workflow_run(actor=actor, run_id=job.workflow_run_id)
            existing = await tx.repository.get_proposal_by_run_id(
                actor=actor, run_id=job.workflow_run_id
            )
            if revision:
                preflight = await tx.repository.get_ai_revision_preflight(
                    actor=actor,
                    proposal_id=UUID(str(job.payload["proposal_id"])),
                    base_version=int(job.payload["base_version"]),
                )
                if preflight is None or preflight.run.id != job.workflow_run_id:
                    return
            if existing is None or revision:
                current, model_context = await tx.repository.load_risk_plan(
                    actor=actor, binding=metadata.binding
                )
                if current != metadata:
                    raise ProposalStaleError
            await tx.commit()
        if existing is not None and not revision:
            return
        if revision:
            assert preflight is not None
            model_context["current_proposal_tasks"] = preflight.version.content["tasks"]
        if run is None:
            raise ProposalStaleError
        request = StructuredModelRequest(
            invocation_key=f"planning.{job.payload['locale']}.risk_replan",
            messages=(
                ModelMessage(
                    role="system",
                    content=RISK_REPLAN_SYSTEM_PROMPT,
                ),
                ModelMessage(
                    role="user",
                    content=json.dumps(
                        {
                            "instruction": job.payload["instruction"],
                            "locale": job.payload["locale"],
                            "context": model_context,
                        },
                        ensure_ascii=False,
                    ),
                ),
            ),
            output_schema=WeeklyReplanModelOutput,
            timeout_seconds=90,
            max_output_tokens=4000,
        )
        if (
            sum(len(m.content.encode()) for m in request.messages)
            + len(json.dumps(request.output_schema.model_json_schema()).encode())
            + 1024
            > 24000
        ):
            raise ValueError("RISK_REPLAN_INPUT_BUDGET")
        response = await self.gateways(run).generate_structured(request)
        content = draft_content(
            metadata, RiskWeeklyDraft.model_validate(response.parsed.model_dump(mode="json"))
        )
        actor = await self.actors.resolve(
            organization_id=job.organization_id, membership_id=actor.membership_id
        )
        if actor is None or actor.role not in {MembershipRole.MANAGER, MembershipRole.ADMIN}:
            raise PlanningRunForbiddenError
        async with self.transactions(actor) as tx:
            await tx.repository.verify_risk_plan(actor=actor, metadata=metadata)
            if revision:
                await tx.repository.finalize_ai_revision_mutation(
                    actor=actor,
                    proposal_id=UUID(str(job.payload["proposal_id"])),
                    base_version=int(job.payload["base_version"]),
                    content=content,
                    change_summary=response.parsed.change_summary,
                    model_reference=response.model_ref,
                    validation_result={"can_approve": True, "errors": [], "warnings": []},
                    request_id=f"risk-replan-revision:{job.id}",
                    idempotency_key=str(job.id),
                )
                await tx.commit()
                return
            existing = await tx.repository.get_proposal_by_run_id(actor=actor, run_id=run.id)
            if existing is not None:
                return
            proposal = Proposal.create(
                organization_id=actor.organization_id, workflow_run_id=run.id
            )
            version = ProposalVersion(
                id=uuid4(),
                organization_id=actor.organization_id,
                proposal_id=proposal.id,
                version_number=1,
                created_by_membership_id=actor.membership_id,
                content=content,
                assumptions=[],
                change_summary=response.parsed.change_summary,
                validation_result={"can_approve": True, "errors": [], "warnings": []},
                source_reference_snapshot=[
                    {
                        "resource_type": "RISK_WEEKLY_PLAN",
                        "resource_id": str(metadata.before.project_id),
                        "metadata": metadata.model_dump(mode="json"),
                    }
                ],
                workflow_version="risk-replan.v1",
                prompt_version="risk-replan.v1",
                schema_version="risk-weekly-plan.v1",
                model_reference=response.model_ref,
                verifier_version="risk-weekly-plan.v1",
                creator_type="AI_SYSTEM",
                field_provenance={"default": "AI_PROPOSED"},
            )
            await tx.repository.create_proposal(proposal=proposal, initial_version=version)
            ready = await tx.repository.complete_proposal_revalidation(
                actor=actor,
                proposal_id=proposal.id,
                version_number=1,
                validation_result=version.validation_result,
                request_id=f"risk-replan:{job.id}",
            )
            running = await tx.repository.update_workflow_run(actor=actor, run=run.mark_running())
            await tx.repository.update_workflow_run(
                actor=actor, run=running.mark_waiting_for_decision()
            )
            await tx.repository.save_checkpoint(
                checkpoint=WorkflowCheckpoint(
                    id=uuid4(),
                    organization_id=actor.organization_id,
                    workflow_run_id=run.id,
                    node="await_manager_decision",
                    sequence=1,
                    state={
                        "locale": job.payload["locale"],
                        "proposal_id": str(proposal.id),
                        "proposal_version": 1,
                    },
                )
            )
            await tx.repository.append_event(
                event=WorkflowEvent(
                    id=uuid4(),
                    organization_id=actor.organization_id,
                    workflow_run_id=run.id,
                    sequence=0,
                    event_type="proposal.ready",
                    public_payload={
                        "proposal_id": str(proposal.id),
                        "version": 1,
                        "approval_id": str(ready.approval_id),
                        "can_approve": True,
                        "error_codes": [],
                    },
                )
            )
            await tx.commit()
