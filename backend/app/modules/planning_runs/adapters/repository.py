"""PostgreSQL repository adapter for AI planning runs persistence."""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.audit.domain.events import AuditOutcome
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.adapters.database_models import MembershipModel
from app.modules.organization.domain.roles import MembershipRole
from app.modules.planning_runs.adapters.database_models import (
    ApprovalModel,
    ContextReferenceModel,
    ModelInvocationModel,
    OutboxEventModel,
    ProposalModel,
    ProposalVersionModel,
    WorkflowCheckpointModel,
    WorkflowEventModel,
    WorkflowJobModel,
    WorkflowRunModel,
)
from app.modules.planning_runs.adapters.risk_replanning import RiskPlanPersistence
from app.modules.planning_runs.application.approval_ports import (
    ApprovalDecision,
    ApprovalDecisionResult,
    CreatedBusinessIds,
)
from app.modules.planning_runs.application.ports import (
    PlanningRunRepository,
    PlanningRuntimePort,
    ProposalAIRevisionPreflight,
    ProposalMutationResult,
    ProposalRevisionRequestResult,
    WorkflowRunMutationResult,
)
from app.modules.planning_runs.domain.models import (
    Approval,
    ApprovalStateConflictError,
    ApprovalStatus,
    ContextReference,
    IdempotencyKeyReusedError,
    InvalidTransitionError,
    ModelInvocation,
    OutboxEvent,
    OutboxStatus,
    PlanningRunDomainError,
    PlanningRunNotFoundError,
    Proposal,
    ProposalStaleError,
    ProposalStatus,
    ProposalValidationError,
    ProposalVersion,
    ResourceVersionMismatchError,
    WorkflowCheckpoint,
    WorkflowEvent,
    WorkflowJob,
    WorkflowJobStatus,
    WorkflowRun,
    WorkflowRunStatus,
)
from app.modules.planning_runs.domain.risk_replanning import RiskPlanBinding, RiskPlanMetadata
from app.modules.progress.adapters.progress_repository import capture_project_baselines
from app.modules.work.adapters.database_models import (
    IdempotencyRecordModel,
    IdempotencyState,
    ProjectModel,
    TaskModel,
)
from app.modules.work.application.shared_commands import build_project_draft, build_task_draft
from app.modules.work.domain.projects import ProjectError
from app.modules.work.domain.tasks import TaskError, TaskStatus
from app.modules.work.planning.adapters.database_models import (
    AcceptanceCriterionModel,
    GoalModel,
    MilestoneModel,
    ProjectWeekModel,
    TaskDependencyModel,
)
from app.modules.work.planning.domain.acceptance_criteria import (
    AcceptanceCriterionDraft,
    AcceptanceCriterionError,
)
from app.modules.work.planning.domain.dependencies import DependencyError, TaskDependencyDraft
from app.modules.work.planning.domain.goals import GoalDraft, GoalError
from app.modules.work.planning.domain.milestones import MilestoneDraft, MilestoneError
from app.modules.work.planning.domain.project_weeks import (
    ProjectWeekDraft,
    ProjectWeekError,
    ProjectWeekStatus,
)

_IDEMPOTENCY_TTL = timedelta(hours=24)


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProposalValidationError(field)
    return cast(dict[str, object], value)


def _items(value: object, field: str) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ProposalValidationError(field)
    return [_mapping(item, field) for item in cast(list[object], value)]


def _optional_date(value: object, field: str) -> date | None:
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise ProposalValidationError(field) from error


def _decision_result(body: dict[str, Any], *, replayed: bool) -> ApprovalDecisionResult:
    created = cast(dict[str, Any], body.get("created", {}))
    return ApprovalDecisionResult(
        approval_id=UUID(str(body["approval_id"])),
        approval_status=ApprovalStatus(str(body["approval_status"])),
        proposal_id=UUID(str(body["proposal_id"])),
        proposal_version=int(body["proposal_version"]),
        proposal_status=ProposalStatus(str(body["proposal_status"])),
        workflow_run_id=UUID(str(body["workflow_run_id"])),
        finalization_job_id=UUID(str(body["finalization_job_id"])),
        created=CreatedBusinessIds(
            project_id=(
                UUID(str(created["project_id"])) if created.get("project_id") is not None else None
            ),
            goal_id=(UUID(str(created["goal_id"])) if created.get("goal_id") is not None else None),
            milestone_ids=tuple(UUID(str(value)) for value in created.get("milestone_ids", [])),
            task_ids=tuple(UUID(str(value)) for value in created.get("task_ids", [])),
            dependency_ids=tuple(UUID(str(value)) for value in created.get("dependency_ids", [])),
            acceptance_criterion_ids=tuple(
                UUID(str(value)) for value in created.get("acceptance_criterion_ids", [])
            ),
        ),
        replayed=replayed,
    )


class PostgreSQLPlanningRunRepository(PlanningRunRepository):
    """PostgreSQL implementation of PlanningRunRepository with RLS and tenant scoping."""

    def __init__(self, session: AsyncSession, reporting_timezone: str = "UTC") -> None:
        self._reporting_timezone = reporting_timezone
        self._session = session

    async def _find_replay(
        self,
        *,
        actor: AuthenticatedActor,
        operation: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> dict[str, Any] | None:
        record = await self._session.scalar(
            select(IdempotencyRecordModel).where(
                IdempotencyRecordModel.organization_id == actor.organization_id,
                IdempotencyRecordModel.actor_membership_id == actor.membership_id,
                IdempotencyRecordModel.operation == operation,
                IdempotencyRecordModel.idempotency_key == idempotency_key,
            )
        )
        if record is None:
            return None
        if (
            record.request_fingerprint != request_fingerprint
            or record.state is not IdempotencyState.COMPLETED
            or record.response_body is None
        ):
            raise IdempotencyKeyReusedError
        return record.response_body

    def _new_idempotency(
        self,
        *,
        actor: AuthenticatedActor,
        operation: str,
        idempotency_key: str,
        request_fingerprint: str,
        now: datetime,
    ) -> IdempotencyRecordModel:
        record = IdempotencyRecordModel(
            id=uuid4(),
            organization_id=actor.organization_id,
            actor_membership_id=actor.membership_id,
            operation=operation,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            state=IdempotencyState.IN_PROGRESS,
            response_status=None,
            response_body=None,
            expires_at=now + _IDEMPOTENCY_TTL,
        )
        self._session.add(record)
        return record

    def _audit_success(
        self,
        *,
        actor: AuthenticatedActor,
        action: str,
        resource_type: str,
        resource_id: UUID,
        request_id: str,
        idempotency_key: str,
        after_data: dict[str, object],
    ) -> None:
        self._session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=actor.organization_id,
                actor_membership_id=actor.membership_id,
                action=action,
                outcome=AuditOutcome.SUCCEEDED,
                resource_type=resource_type,
                resource_id=resource_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                before_data={},
                after_data=after_data,
                reason_data={},
            )
        )

    async def audit_rejection(
        self,
        *,
        actor: AuthenticatedActor,
        action: str,
        request_id: str,
        reason_code: str,
        idempotency_key: str | None = None,
        resource_id: UUID | None = None,
        **_: object,
    ) -> None:
        self._session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=actor.organization_id,
                actor_membership_id=actor.membership_id,
                action=action,
                outcome=AuditOutcome.REJECTED,
                resource_type="ai_planning",
                resource_id=resource_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                before_data={},
                after_data={},
                reason_data={"code": reason_code},
            )
        )

    async def decide_approval_mutation(self, **values: object) -> ApprovalDecisionResult:
        """Lock, revalidate and apply one exact immutable proposal version."""

        actor = values["actor"]
        approval_id = values["approval_id"]
        decision = values["decision"]
        runtime = values["runtime"]
        assert isinstance(actor, AuthenticatedActor)
        assert isinstance(approval_id, UUID)
        assert isinstance(decision, ApprovalDecision)
        assert hasattr(runtime, "validate_proposal_content")
        typed_runtime = cast(PlanningRuntimePort, runtime)
        expected_version = cast(int, values["expected_proposal_version"])
        reason = cast(str | None, values["reason"])
        request_id = str(values["request_id"])
        idempotency_key = str(values["idempotency_key"])
        request_fingerprint = str(values["request_fingerprint"])
        operation = f"approval.decision:{approval_id}"

        decision_content = await self._session.scalar(
            select(ProposalVersionModel.content)
            .join(
                ApprovalModel,
                (
                    (ApprovalModel.organization_id == ProposalVersionModel.organization_id)
                    & (ApprovalModel.proposal_id == ProposalVersionModel.proposal_id)
                    & (ApprovalModel.proposal_version_number == ProposalVersionModel.version_number)
                ),
            )
            .where(
                ApprovalModel.organization_id == actor.organization_id,
                ApprovalModel.id == approval_id,
            )
        )
        if decision_content is not None and "risk_replan" in decision_content:
            await self.authenticate_risk_plan_actor(actor=actor)

        replay = await self._find_replay(
            actor=actor,
            operation=operation,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            return _decision_result(replay, replayed=True)

        approval_model = await self._session.scalar(
            select(ApprovalModel)
            .where(
                ApprovalModel.organization_id == actor.organization_id,
                ApprovalModel.id == approval_id,
            )
            .with_for_update()
        )
        if approval_model is None:
            raise PlanningRunNotFoundError

        # A concurrent same-key request may have completed while this request
        # waited on the approval lock. Re-read after acquiring the final lock.
        replay = await self._find_replay(
            actor=actor,
            operation=operation,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            return _decision_result(replay, replayed=True)
        if approval_model.status != ApprovalStatus.PENDING.value:
            raise ApprovalStateConflictError

        proposal_model = await self._session.scalar(
            select(ProposalModel)
            .where(
                ProposalModel.organization_id == actor.organization_id,
                ProposalModel.id == approval_model.proposal_id,
            )
            .with_for_update()
        )
        if proposal_model is None:
            raise PlanningRunNotFoundError
        if expected_version != proposal_model.current_version_number:
            raise ResourceVersionMismatchError(proposal_model.current_version_number)
        if approval_model.proposal_version_number != expected_version:
            raise ResourceVersionMismatchError(proposal_model.current_version_number)
        if (
            proposal_model.status != ProposalStatus.READY_FOR_DECISION.value
            or proposal_model.approval_id != approval_id
        ):
            raise ApprovalStateConflictError

        version_lock_key = (
            f"planning-proposal-version:{actor.organization_id}:"
            f"{proposal_model.id}:{expected_version}"
        )
        await self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(version_lock_key, 0)))
        )
        version_model = await self._session.scalar(
            select(ProposalVersionModel).where(
                ProposalVersionModel.organization_id == actor.organization_id,
                ProposalVersionModel.proposal_id == proposal_model.id,
                ProposalVersionModel.version_number == expected_version,
            )
        )
        if version_model is None:
            raise PlanningRunNotFoundError
        run_model = await self._session.scalar(
            select(WorkflowRunModel)
            .where(
                WorkflowRunModel.organization_id == actor.organization_id,
                WorkflowRunModel.id == proposal_model.workflow_run_id,
            )
            .with_for_update()
        )
        if run_model is None:
            raise PlanningRunNotFoundError
        if run_model.status != WorkflowRunStatus.WAITING_FOR_DECISION.value:
            raise ApprovalStateConflictError
        checkpoint = await self._session.scalar(
            select(WorkflowCheckpointModel)
            .where(
                WorkflowCheckpointModel.organization_id == actor.organization_id,
                WorkflowCheckpointModel.workflow_run_id == run_model.id,
            )
            .order_by(WorkflowCheckpointModel.sequence.desc())
            .limit(1)
            .with_for_update()
        )
        if checkpoint is None or checkpoint.node != "await_manager_decision":
            raise ApprovalStateConflictError

        await self._verify_source_freshness(
            actor=actor,
            snapshot=version_model.source_reference_snapshot,
        )
        if not bool(version_model.validation_result.get("can_approve", False)):
            raise ProposalValidationError

        normalized = typed_runtime.validate_proposal_content(version_model.content)
        current_validation = typed_runtime.validate_proposal_deterministically(
            normalized,
            active_membership_ids=frozenset(),
        )
        if not bool(current_validation.get("can_approve", False)):
            raise ProposalValidationError

        now = datetime.now(UTC)
        idempotency = self._new_idempotency(
            actor=actor,
            operation=operation,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            now=now,
        )
        created = CreatedBusinessIds()
        if decision is ApprovalDecision.APPROVE:
            try:
                created = await self._apply_business_graph(
                    actor=actor,
                    content=normalized,
                    now=now,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                )
            except (
                AcceptanceCriterionError,
                DependencyError,
                GoalError,
                MilestoneError,
                ProjectError,
                ProjectWeekError,
                TaskError,
            ) as error:
                raise ProposalValidationError from error

        approval_model.status = (
            ApprovalStatus.APPROVED.value
            if decision is ApprovalDecision.APPROVE
            else ApprovalStatus.REJECTED.value
        )
        approval_model.decided_by_membership_id = actor.membership_id
        approval_model.decision_reason = reason
        approval_model.decided_at = now
        approval_model.version += 1
        approval_model.updated_at = now
        proposal_model.status = (
            ProposalStatus.APPROVED.value
            if decision is ApprovalDecision.APPROVE
            else ProposalStatus.REJECTED.value
        )
        proposal_model.version += 1
        proposal_model.updated_at = now

        finalization_job_id = uuid4()
        self._session.add(
            WorkflowJobModel(
                id=finalization_job_id,
                organization_id=actor.organization_id,
                workflow_run_id=run_model.id,
                job_type="planning.finalize",
                status=WorkflowJobStatus.QUEUED.value,
                payload={
                    "instruction": "FINALIZE_MANAGER_DECISION",
                    "approval_id": str(approval_id),
                    "proposal_id": str(proposal_model.id),
                    "proposal_version": expected_version,
                    "decision": decision.value,
                    "checkpoint_sequence": checkpoint.sequence,
                    "project_id": str(created.project_id) if created.project_id else None,
                },
                attempt_count=0,
                max_attempts=3,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        if decision is ApprovalDecision.APPROVE:
            outbox_id = uuid4()
            self._session.add(
                OutboxEventModel(
                    id=outbox_id,
                    event_id=outbox_id,
                    organization_id=actor.organization_id,
                    event_type="planning.proposal_approved.v1",
                    aggregate_type="proposal",
                    aggregate_id=proposal_model.id,
                    payload={
                        "envelope_version": "1.0",
                        "organization_id": str(actor.organization_id),
                        "workflow_run_id": str(run_model.id),
                        "proposal_id": str(proposal_model.id),
                        "proposal_version": expected_version,
                        "approval_id": str(approval_id),
                        "created": self._created_json(created),
                    },
                    status=OutboxStatus.PENDING.value,
                    envelope_version="1.0",
                    attempt_count=0,
                    max_attempts=3,
                    available_at=now,
                    occurred_at=now,
                    created_at=now,
                )
            )
        self._audit_success(
            actor=actor,
            action="approval.decided",
            resource_type="approval",
            resource_id=approval_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
            after_data={
                "decision": decision.value,
                "proposal_id": str(proposal_model.id),
                "proposal_version": expected_version,
                "created": self._created_json(created),
            },
        )
        body: dict[str, Any] = {
            "approval_id": str(approval_id),
            "approval_status": approval_model.status,
            "proposal_id": str(proposal_model.id),
            "proposal_version": expected_version,
            "proposal_status": proposal_model.status,
            "workflow_run_id": str(run_model.id),
            "finalization_job_id": str(finalization_job_id),
            "created": self._created_json(created),
        }
        idempotency.state = IdempotencyState.COMPLETED
        idempotency.response_status = 200
        idempotency.response_body = body
        await self._session.flush()
        return _decision_result(body, replayed=False)

    async def mark_stale_decision_attempt(self, **values: object) -> None:
        """Persist STALE/superseded lifecycle and safe audit after apply rollback."""

        actor = values["actor"]
        approval_id = values["approval_id"]
        assert isinstance(actor, AuthenticatedActor)
        assert isinstance(approval_id, UUID)
        expected_version = cast(int, values["expected_proposal_version"])
        approval = await self._session.scalar(
            select(ApprovalModel)
            .where(
                ApprovalModel.organization_id == actor.organization_id,
                ApprovalModel.id == approval_id,
            )
            .with_for_update()
        )
        if approval is None:
            return
        proposal = await self._session.scalar(
            select(ProposalModel)
            .where(
                ProposalModel.organization_id == actor.organization_id,
                ProposalModel.id == approval.proposal_id,
            )
            .with_for_update()
        )
        if (
            proposal is not None
            and approval.status == ApprovalStatus.PENDING.value
            and approval.proposal_version_number == expected_version
            and proposal.status == ProposalStatus.READY_FOR_DECISION.value
            and proposal.current_version_number == expected_version
            and proposal.approval_id == approval.id
        ):
            now = datetime.now(UTC)
            approval.status = ApprovalStatus.SUPERSEDED.value
            approval.version += 1
            approval.updated_at = now
            proposal.status = ProposalStatus.STALE.value
            proposal.approval_id = None
            proposal.superseded_approval_id = approval.id
            proposal.version += 1
            proposal.updated_at = now
        await self.audit_rejection(
            actor=actor,
            action="approval.decided",
            request_id=str(values["request_id"]),
            reason_code="PROPOSAL_STALE",
            idempotency_key=str(values["idempotency_key"]),
            resource_id=approval_id,
        )
        await self._session.flush()

    @staticmethod
    def _created_json(created: CreatedBusinessIds) -> dict[str, object]:
        return {
            "project_id": str(created.project_id) if created.project_id else None,
            "goal_id": str(created.goal_id) if created.goal_id else None,
            "milestone_ids": [str(value) for value in created.milestone_ids],
            "task_ids": [str(value) for value in created.task_ids],
            "dependency_ids": [str(value) for value in created.dependency_ids],
            "acceptance_criterion_ids": [str(value) for value in created.acceptance_criterion_ids],
        }

    async def fail_risk_revision_run(self, *, organization_id: UUID, run_id: UUID) -> bool:
        model = await self._session.scalar(
            select(WorkflowRunModel)
            .where(
                WorkflowRunModel.organization_id == organization_id, WorkflowRunModel.id == run_id
            )
            .with_for_update()
        )
        if model is None or model.status not in {"QUEUED", "RUNNING"}:
            return False
        model.status = "FAILED"
        model.error_message = "RISK_REPLAN_UNAVAILABLE"
        model.version += 1
        model.updated_at = datetime.now(UTC)
        await self._session.flush()
        return True

    async def authenticate_risk_plan_actor(self, *, actor: AuthenticatedActor) -> None:
        from app.modules.planning_runs.domain.models import PlanningRunForbiddenError
        from app.modules.progress.domain.blockers import BlockerError
        from app.modules.risk.adapters.repository import RiskRepository

        try:
            await RiskRepository(self._session, actor, self._reporting_timezone).authenticate()
        except BlockerError as error:
            raise PlanningRunForbiddenError from error

    async def load_risk_plan(
        self, *, actor: AuthenticatedActor, binding: RiskPlanBinding
    ) -> tuple[RiskPlanMetadata, dict[str, object]]:
        from app.modules.progress.domain.blockers import BlockerError

        try:
            metadata = await RiskPlanPersistence(self._session, self._reporting_timezone).load(
                actor, binding
            )
        except BlockerError as error:
            raise PlanningRunNotFoundError from error
        from app.modules.risk.adapters.repository import RiskRepository

        context = await RiskRepository(self._session, actor, self._reporting_timezone).read_context(
            binding.task_id
        )
        return metadata, {
            "plan": metadata.before.model_dump(mode="json"),
            "risk": context.model_dump(mode="json"),
        }

    async def verify_risk_plan(
        self, *, actor: AuthenticatedActor, metadata: RiskPlanMetadata
    ) -> None:
        await self.authenticate_risk_plan_actor(actor=actor)
        await RiskPlanPersistence(self._session, self._reporting_timezone).verify(actor, metadata)

    async def verify_risk_plan_content(
        self, *, actor: AuthenticatedActor, content: dict[str, object]
    ) -> None:
        await self.verify_risk_plan(
            actor=actor, metadata=RiskPlanMetadata.model_validate(content["risk_replan"])
        )

    async def get_risk_revision_job(
        self, *, actor: AuthenticatedActor, run_id: UUID
    ) -> WorkflowJob | None:
        model = await self._session.scalar(
            select(WorkflowJobModel).where(
                WorkflowJobModel.organization_id == actor.organization_id,
                WorkflowJobModel.workflow_run_id == run_id,
                WorkflowJobModel.job_type == "proposal.risk_replan",
            )
        )
        if model is None:
            return None
        return WorkflowJob(
            id=model.id,
            organization_id=model.organization_id,
            workflow_run_id=model.workflow_run_id,
            job_type=model.job_type,
            status=WorkflowJobStatus(model.status),
            payload=model.payload,
            attempt_count=model.attempt_count,
            max_attempts=model.max_attempts,
        )

    async def _verify_source_freshness(
        self,
        *,
        actor: AuthenticatedActor,
        snapshot: list[dict[str, Any]],
    ) -> None:
        for raw in snapshot:
            item = dict(raw)
            if set(item) == {"reference_id"}:
                try:
                    reference_id = UUID(str(item["reference_id"]))
                except ValueError as error:
                    raise ProposalStaleError from error
                reference = await self._session.scalar(
                    select(ContextReferenceModel).where(
                        ContextReferenceModel.organization_id == actor.organization_id,
                        ContextReferenceModel.id == reference_id,
                    )
                )
                if reference is None or reference.provenance_notes is None:
                    raise ProposalStaleError
                try:
                    provenance = json.loads(reference.provenance_notes)
                except json.JSONDecodeError as error:
                    raise ProposalStaleError from error
                item = {
                    **cast(dict[str, Any], provenance),
                    "resource_type": reference.resource_type,
                    "resource_id": str(reference.resource_id),
                }
            resource_type = str(item.get("resource_type", "")).upper()
            try:
                resource_id = UUID(str(item["resource_id"]))
            except (KeyError, ValueError) as error:
                raise ProposalStaleError from error
            if resource_type == "RISK_WEEKLY_PLAN":
                metadata = RiskPlanMetadata.model_validate(item["metadata"])
                if metadata.before.project_id != resource_id:
                    raise ProposalStaleError
                await self.verify_risk_plan(actor=actor, metadata=metadata)
            elif resource_type == "PROJECT":
                model = await self._session.scalar(
                    select(ProjectModel).where(
                        ProjectModel.organization_id == actor.organization_id,
                        ProjectModel.id == resource_id,
                    )
                )
                if model is None or int(item.get("version", -1)) != model.version:
                    raise ProposalStaleError
            elif resource_type == "TASK":
                task = await self._session.scalar(
                    select(TaskModel).where(
                        TaskModel.organization_id == actor.organization_id,
                        TaskModel.id == resource_id,
                    )
                )
                if task is None or int(item.get("version", -1)) != task.version:
                    raise ProposalStaleError
            elif resource_type == "MEMBERSHIP":
                membership = await self._session.scalar(
                    select(MembershipModel).where(
                        MembershipModel.organization_id == actor.organization_id,
                        MembershipModel.id == resource_id,
                    )
                )
                if membership is None or not membership.is_active:
                    raise ProposalStaleError
                expected = item.get("fingerprint")
                current = hashlib.sha256(
                    json.dumps(
                        {
                            "id": str(membership.id),
                            "role": membership.role.value,
                            "is_active": membership.is_active,
                            "updated_at": membership.updated_at.isoformat(),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                if expected != current:
                    raise ProposalStaleError
            else:
                raise ProposalStaleError

    async def _apply_business_graph(
        self,
        *,
        actor: AuthenticatedActor,
        content: dict[str, object],
        now: datetime,
        request_id: str = "approved-plan-capture",
        idempotency_key: str | None = None,
    ) -> CreatedBusinessIds:
        if "risk_replan" in content:
            return await RiskPlanPersistence(self._session, self._reporting_timezone).apply(
                actor, content, request_id, idempotency_key
            )
        project_data = _mapping(content.get("project"), "project")
        goal_data = _mapping(content.get("goal"), "goal")
        milestone_data = _items(content.get("milestones"), "milestones")
        if "project_weeks" not in content:
            raise ProposalValidationError("PROPOSAL_SCHEMA_UPGRADE_REQUIRED")
        project_week_data = _items(content.get("project_weeks"), "project_weeks")
        task_data = _items(content.get("tasks"), "tasks")
        dependency_data = _items(content.get("dependencies"), "dependencies")
        project_start = _optional_date(project_data.get("start_date"), "project.start_date")
        project_due = _optional_date(project_data.get("due_date"), "project.due_date")
        goal_target = _optional_date(goal_data.get("target_date"), "goal.target_date")
        if project_start is not None and project_due is not None and project_start > project_due:
            raise ProposalValidationError
        if goal_target is not None and project_due is not None and goal_target > project_due:
            raise ProposalValidationError

        project_draft = build_project_draft(
            name=str(project_data.get("title", "")),
            description=cast(str | None, project_data.get("description")),
        )
        project_id = uuid4()
        self._session.add(
            ProjectModel(
                id=project_id,
                organization_id=actor.organization_id,
                name=project_draft.name,
                description=project_draft.description,
                version=1,
                created_by_membership_id=actor.membership_id,
                updated_by_membership_id=actor.membership_id,
                created_at=now,
                updated_at=now,
            )
        )
        await self._session.flush()
        goal_draft = GoalDraft.create(
            project_id=project_id,
            title=str(goal_data.get("title", "")),
            description=cast(str | None, goal_data.get("description")),
            expected_outcomes=tuple(
                str(value) for value in cast(list[object], goal_data.get("expected_outcomes", []))
            ),
            target_date=goal_target,
        )
        goal_id = uuid4()
        self._session.add(
            GoalModel(
                id=goal_id,
                organization_id=actor.organization_id,
                project_id=project_id,
                title=goal_draft.title,
                description=goal_draft.description,
                expected_outcomes=list(goal_draft.expected_outcomes),
                target_date=goal_draft.target_date,
                version=1,
                created_by_membership_id=actor.membership_id,
                updated_by_membership_id=actor.membership_id,
                created_at=now,
                updated_at=now,
            )
        )

        milestone_ids: list[UUID] = []
        milestone_by_ref: dict[str, UUID] = {}
        milestone_due_by_ref: dict[str, date | None] = {}
        for position, raw in enumerate(milestone_data, start=1):
            ref = str(raw.get("ref", ""))
            if not ref or ref in milestone_by_ref:
                raise ProposalValidationError
            due = _optional_date(raw.get("due_date"), f"milestones[{ref}].due_date")
            if due is not None and project_due is not None and due > project_due:
                raise ProposalValidationError
            draft = MilestoneDraft.create(
                project_id=project_id,
                name=str(raw.get("title", "")),
                description=cast(str | None, raw.get("description")),
                target_date=due,
                position=position,
            )
            milestone_id = uuid4()
            milestone_ids.append(milestone_id)
            milestone_by_ref[ref] = milestone_id
            milestone_due_by_ref[ref] = due
            self._session.add(
                MilestoneModel(
                    id=milestone_id,
                    organization_id=actor.organization_id,
                    project_id=project_id,
                    name=draft.name,
                    description=draft.description,
                    target_date=draft.target_date,
                    position=draft.position,
                    version=1,
                    created_by_membership_id=actor.membership_id,
                    updated_by_membership_id=actor.membership_id,
                    created_at=now,
                    updated_at=now,
                )
            )
        await self._session.flush()

        project_week_by_ref: dict[str, UUID] = {}
        previous_end: date | None = None
        expected_week_number = 1

        def week_number_value(item: dict[str, object]) -> int:
            value = item.get("week_number", 0)
            if not isinstance(value, (int, str)):
                raise ProposalValidationError
            return int(value)

        for raw in sorted(project_week_data, key=week_number_value):
            ref = str(raw.get("ref", ""))
            try:
                week_number = week_number_value(raw)
            except (TypeError, ValueError) as error:
                raise ProposalValidationError from error
            if not ref or ref in project_week_by_ref or week_number != expected_week_number:
                raise ProposalValidationError
            start_date = _optional_date(raw.get("start_date"), f"project_weeks[{ref}].start_date")
            end_date = _optional_date(raw.get("end_date"), f"project_weeks[{ref}].end_date")
            if start_date is None or end_date is None:
                raise ProposalValidationError
            if previous_end is not None and previous_end >= start_date:
                raise ProposalValidationError
            if project_start is not None and start_date < project_start:
                raise ProposalValidationError
            if project_due is not None and end_date > project_due:
                raise ProposalValidationError
            draft = ProjectWeekDraft.create(
                project_id=project_id,
                week_number=week_number,
                start_date=start_date,
                end_date=end_date,
                objective=str(raw.get("objective", "")),
                status=ProjectWeekStatus.PLANNED,
            )
            project_week_id = uuid4()
            project_week_by_ref[ref] = project_week_id
            self._session.add(
                ProjectWeekModel(
                    id=project_week_id,
                    organization_id=actor.organization_id,
                    project_id=project_id,
                    week_number=draft.week_number,
                    start_date=draft.start_date,
                    end_date=draft.end_date,
                    objective=draft.objective,
                    status=draft.status,
                    version=1,
                    created_by_membership_id=actor.membership_id,
                    updated_by_membership_id=actor.membership_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            previous_end = end_date
            expected_week_number += 1
        await self._session.flush()

        task_ids: list[UUID] = []
        task_by_ref: dict[str, UUID] = {}
        criterion_ids: list[UUID] = []
        for raw in task_data:
            ref = str(raw.get("ref", ""))
            if not ref or ref in task_by_ref:
                raise ProposalValidationError
            milestone_ref_value = raw.get("milestone_ref")
            milestone_ref = str(milestone_ref_value) if milestone_ref_value is not None else None
            if milestone_ref is not None and milestone_ref not in milestone_by_ref:
                raise ProposalValidationError
            project_week_ref = str(raw.get("project_week_ref", ""))
            if project_week_ref not in project_week_by_ref:
                raise ProposalValidationError
            due = _optional_date(raw.get("due_date"), f"tasks[{ref}].due_date")
            if (
                milestone_ref is not None
                and due is not None
                and milestone_due_by_ref[milestone_ref] is not None
                and due > cast(date, milestone_due_by_ref[milestone_ref])
            ):
                raise ProposalValidationError
            if raw.get("assignee_membership_id") is not None:
                raise ProposalValidationError
            skill_values = raw.get("required_skill_labels")
            if not isinstance(skill_values, list):
                raise ProposalValidationError
            try:
                effort_value = raw["estimated_effort_hours"]
                if not isinstance(effort_value, (int, str)):
                    raise ProposalValidationError
                effort = int(effort_value)
            except (KeyError, TypeError, ValueError) as error:
                raise ProposalValidationError from error
            draft = build_task_draft(
                project_id=project_id,
                project_week_id=project_week_by_ref[project_week_ref],
                milestone_id=(
                    milestone_by_ref[milestone_ref] if milestone_ref is not None else None
                ),
                title=str(raw.get("title", "")),
                description=cast(str | None, raw.get("description")),
                assignee_membership_id=None,
                required_skill_labels=tuple(
                    str(value) for value in cast(list[object], skill_values)
                ),
                estimated_effort_hours=effort,
                due_date=due,
            )
            task_id = uuid4()
            task_ids.append(task_id)
            task_by_ref[ref] = task_id
            self._session.add(
                TaskModel(
                    id=task_id,
                    organization_id=actor.organization_id,
                    project_id=project_id,
                    project_week_id=draft.project_week_id,
                    milestone_id=draft.milestone_id,
                    title=draft.title,
                    description=draft.description,
                    assignee_membership_id=draft.assignee_membership_id,
                    required_skill_labels=list(draft.required_skill_labels),
                    estimated_effort_hours=draft.estimated_effort_hours,
                    status=TaskStatus.TO_DO,
                    due_date=draft.due_date,
                    version=1,
                    created_by_membership_id=actor.membership_id,
                    updated_by_membership_id=actor.membership_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            criteria = raw.get("acceptance_criteria", [])
            if not isinstance(criteria, list):
                raise ProposalValidationError
            normalized_criteria: set[str] = set()
            for position, text_value in enumerate(cast(list[object], criteria), start=1):
                criterion = AcceptanceCriterionDraft.create(
                    task_id=task_id,
                    text=str(text_value),
                    position=position,
                )
                normalized = " ".join(criterion.text.split()).casefold()
                if normalized in normalized_criteria:
                    raise ProposalValidationError
                normalized_criteria.add(normalized)
                criterion_id = uuid4()
                criterion_ids.append(criterion_id)
                self._session.add(
                    AcceptanceCriterionModel(
                        id=criterion_id,
                        organization_id=actor.organization_id,
                        task_id=task_id,
                        text=criterion.text,
                        position=criterion.position,
                        version=1,
                        created_by_membership_id=actor.membership_id,
                        updated_by_membership_id=actor.membership_id,
                        created_at=now,
                        updated_at=now,
                    )
                )

        await self._session.flush()

        dependency_ids: list[UUID] = []
        edge_set: set[tuple[UUID, UUID]] = set()
        for raw in dependency_data:
            predecessor_ref = str(raw.get("predecessor_ref", ""))
            successor_ref = str(raw.get("successor_ref", ""))
            if predecessor_ref not in task_by_ref or successor_ref not in task_by_ref:
                raise ProposalValidationError
            draft = TaskDependencyDraft.create(
                predecessor_task_id=task_by_ref[predecessor_ref],
                successor_task_id=task_by_ref[successor_ref],
            )
            edge = (draft.predecessor_task_id, draft.successor_task_id)
            if edge in edge_set:
                raise ProposalValidationError
            edge_set.add(edge)
            dependency_id = uuid4()
            dependency_ids.append(dependency_id)
            self._session.add(
                TaskDependencyModel(
                    id=dependency_id,
                    organization_id=actor.organization_id,
                    predecessor_task_id=draft.predecessor_task_id,
                    successor_task_id=draft.successor_task_id,
                    version=1,
                    created_by_membership_id=actor.membership_id,
                    updated_by_membership_id=actor.membership_id,
                    created_at=now,
                    updated_at=now,
                )
            )
        await self._session.flush()
        await capture_project_baselines(
            self._session,
            actor,
            project_id,
            "APPROVED",
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
        return CreatedBusinessIds(
            project_id=project_id,
            goal_id=goal_id,
            milestone_ids=tuple(milestone_ids),
            task_ids=tuple(task_ids),
            dependency_ids=tuple(dependency_ids),
            acceptance_criterion_ids=tuple(criterion_ids),
        )

    async def create_planning_run_mutation(self, **values: object) -> WorkflowRunMutationResult:
        actor = values["actor"]
        run = values["run"]
        job = values["job"]
        assert isinstance(actor, AuthenticatedActor)
        assert isinstance(run, WorkflowRun)
        assert isinstance(job, WorkflowJob)
        request_id = str(values["request_id"])
        idempotency_key = str(values["idempotency_key"])
        request_fingerprint = str(values["request_fingerprint"])
        operation = "planning_run.create"
        replay = await self._find_replay(
            actor=actor,
            operation=operation,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            existing = await self.get_workflow_run(actor=actor, run_id=UUID(str(replay["run_id"])))
            if existing is None:
                raise PlanningRunDomainError("idempotency replay resource is unavailable")
            return WorkflowRunMutationResult(run=existing, replayed=True)
        now = datetime.now(UTC)
        record = self._new_idempotency(
            actor=actor,
            operation=operation,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            now=now,
        )
        await self.create_workflow_run(run=run, job=job)
        self._audit_success(
            actor=actor,
            action="planning_run.created",
            resource_type="workflow_run",
            resource_id=run.id,
            request_id=request_id,
            idempotency_key=idempotency_key,
            after_data={"status": run.status.value, "job_type": job.job_type},
        )
        record.state = IdempotencyState.COMPLETED
        record.response_status = 202
        record.response_body = {"run_id": str(run.id)}
        await self._session.flush()
        return WorkflowRunMutationResult(run=run, replayed=False)

    async def list_workflow_runs(
        self, *, actor: AuthenticatedActor, limit: int
    ) -> tuple[WorkflowRun, ...]:
        models = (
            await self._session.scalars(
                select(WorkflowRunModel)
                .where(WorkflowRunModel.organization_id == actor.organization_id)
                .order_by(WorkflowRunModel.created_at.desc(), WorkflowRunModel.id)
                .limit(limit)
            )
        ).all()
        return tuple(self._run_from_model(model) for model in models)

    @staticmethod
    def _run_from_model(model: WorkflowRunModel) -> WorkflowRun:
        return WorkflowRun(
            id=model.id,
            organization_id=model.organization_id,
            project_id=model.project_id,
            requested_by_membership_id=model.requested_by_membership_id,
            status=WorkflowRunStatus(model.status),
            workflow_name=model.workflow_name,
            workflow_version=model.workflow_version,
            verifier_version=model.verifier_version,
            input_goal_text=model.input_goal_text,
            error_message=model.error_message,
            version=model.version,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    async def create_workflow_run(
        self,
        *,
        run: WorkflowRun,
        job: WorkflowJob | None = None,
    ) -> WorkflowRun:
        run_model = WorkflowRunModel(
            id=run.id,
            organization_id=run.organization_id,
            project_id=run.project_id,
            requested_by_membership_id=run.requested_by_membership_id,
            status=run.status.value,
            workflow_name=run.workflow_name,
            workflow_version=run.workflow_version,
            verifier_version=run.verifier_version,
            input_goal_text=run.input_goal_text,
            error_message=run.error_message,
            version=run.version,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )
        self._session.add(run_model)
        await self._session.flush()

        if job is not None:
            job_model = WorkflowJobModel(
                id=job.id,
                organization_id=job.organization_id,
                workflow_run_id=job.workflow_run_id,
                job_type=job.job_type,
                status=job.status.value,
                payload=job.payload,
                attempt_count=job.attempt_count,
                max_attempts=job.max_attempts,
                available_at=job.available_at,
                locked_by_worker_id=job.locked_by_worker_id,
                lease_until=job.lease_until,
                last_error=job.last_error,
                created_at=job.created_at,
                updated_at=job.updated_at,
            )
            self._session.add(job_model)
            await self._session.flush()
        return run

    async def get_workflow_run(
        self,
        *,
        actor: AuthenticatedActor,
        run_id: UUID,
    ) -> WorkflowRun | None:
        stmt = select(WorkflowRunModel).where(
            WorkflowRunModel.id == run_id,
            WorkflowRunModel.organization_id == actor.organization_id,
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return self._run_from_model(model)

    async def get_workflow_run_by_scope(
        self, *, organization_id: UUID, run_id: UUID
    ) -> WorkflowRun | None:
        model = await self._session.scalar(
            select(WorkflowRunModel).where(
                WorkflowRunModel.organization_id == organization_id,
                WorkflowRunModel.id == run_id,
            )
        )
        return self._run_from_model(model) if model is not None else None

    async def list_active_membership_ids(self, *, organization_id: UUID) -> frozenset[UUID]:
        values = (
            await self._session.scalars(
                select(MembershipModel.id).where(
                    MembershipModel.organization_id == organization_id,
                    MembershipModel.is_active.is_(True),
                )
            )
        ).all()
        return frozenset(values)

    async def resume_planning_run_mutation(self, **values: object) -> WorkflowRunMutationResult:
        actor = values["actor"]
        run = values["run"]
        job = values["job"]
        checkpoint = values["checkpoint"]
        assert isinstance(actor, AuthenticatedActor)
        assert isinstance(run, WorkflowRun)
        assert isinstance(job, WorkflowJob)
        assert isinstance(checkpoint, WorkflowCheckpoint)
        request_id = str(values["request_id"])
        idempotency_key = str(values["idempotency_key"])
        request_fingerprint = str(values["request_fingerprint"])
        operation = f"planning_run.message:{run.id}"
        replay = await self._find_replay(
            actor=actor,
            operation=operation,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            existing = await self.get_workflow_run(actor=actor, run_id=run.id)
            if existing is None:
                raise PlanningRunDomainError("idempotency replay resource is unavailable")
            return WorkflowRunMutationResult(run=existing, replayed=True)
        locked = await self._session.scalar(
            select(WorkflowRunModel)
            .where(
                WorkflowRunModel.organization_id == actor.organization_id,
                WorkflowRunModel.id == run.id,
                WorkflowRunModel.status == WorkflowRunStatus.NEEDS_INPUT.value,
                WorkflowRunModel.version == run.version - 1,
            )
            .with_for_update()
        )
        if locked is None:
            raise InvalidTransitionError("run no longer awaits manager input")
        latest = await self._session.scalar(
            select(WorkflowCheckpointModel)
            .where(
                WorkflowCheckpointModel.organization_id == actor.organization_id,
                WorkflowCheckpointModel.workflow_run_id == run.id,
            )
            .order_by(WorkflowCheckpointModel.sequence.desc())
            .limit(1)
            .with_for_update()
        )
        if (
            latest is None
            or latest.sequence != checkpoint.sequence
            or latest.node != "await_manager_input"
        ):
            raise InvalidTransitionError("manager-input checkpoint changed")
        now = datetime.now(UTC)
        record = self._new_idempotency(
            actor=actor,
            operation=operation,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            now=now,
        )
        locked.status = run.status.value
        locked.version = run.version
        locked.updated_at = run.updated_at
        self._session.add(
            WorkflowJobModel(
                id=job.id,
                organization_id=job.organization_id,
                workflow_run_id=job.workflow_run_id,
                job_type=job.job_type,
                status=job.status.value,
                payload=job.payload,
                attempt_count=job.attempt_count,
                max_attempts=job.max_attempts,
                available_at=job.available_at,
                created_at=job.created_at,
                updated_at=job.updated_at,
            )
        )
        self._audit_success(
            actor=actor,
            action="planning_run.message_submitted",
            resource_type="workflow_run",
            resource_id=run.id,
            request_id=request_id,
            idempotency_key=idempotency_key,
            after_data={"status": run.status.value, "checkpoint": checkpoint.sequence},
        )
        record.state = IdempotencyState.COMPLETED
        record.response_status = 202
        record.response_body = {"run_id": str(run.id)}
        await self._session.flush()
        return WorkflowRunMutationResult(run=run, replayed=False)

    async def find_workflow_run_mutation_replay(
        self, **values: object
    ) -> WorkflowRunMutationResult | None:
        actor = values["actor"]
        assert isinstance(actor, AuthenticatedActor)
        replay = await self._find_replay(
            actor=actor,
            operation=str(values["operation"]),
            idempotency_key=str(values["idempotency_key"]),
            request_fingerprint=str(values["request_fingerprint"]),
        )
        if replay is None:
            return None
        run = await self.get_workflow_run(actor=actor, run_id=UUID(str(replay["run_id"])))
        if run is None:
            raise PlanningRunDomainError("idempotency replay resource is unavailable")
        return WorkflowRunMutationResult(run=run, replayed=True)

    async def find_invalid_active_membership_ids(
        self,
        *,
        actor: AuthenticatedActor,
        membership_ids: set[UUID],
    ) -> set[UUID]:
        if not membership_ids:
            return set()
        valid = set(
            (
                await self._session.scalars(
                    select(MembershipModel.id).where(
                        MembershipModel.organization_id == actor.organization_id,
                        MembershipModel.id.in_(membership_ids),
                        MembershipModel.is_active.is_(True),
                    )
                )
            ).all()
        )
        return membership_ids - valid

    async def complete_proposal_revalidation(
        self,
        *,
        actor: AuthenticatedActor,
        proposal_id: UUID,
        version_number: int,
        validation_result: dict[str, object],
        request_id: str,
    ) -> Proposal:
        proposal_model = await self._session.scalar(
            select(ProposalModel)
            .where(
                ProposalModel.organization_id == actor.organization_id,
                ProposalModel.id == proposal_id,
                ProposalModel.status == ProposalStatus.DRAFT.value,
                ProposalModel.current_version_number == version_number,
            )
            .with_for_update()
        )
        version_model = await self._session.scalar(
            select(ProposalVersionModel).where(
                ProposalVersionModel.organization_id == actor.organization_id,
                ProposalVersionModel.proposal_id == proposal_id,
                ProposalVersionModel.version_number == version_number,
            )
        )
        if proposal_model is None or version_model is None:
            raise InvalidTransitionError("proposal version is unavailable for revalidation")
        if version_model.validation_result != validation_result:
            raise InvalidTransitionError("proposal validation snapshot is stale")
        is_valid = bool(validation_result.get("can_approve", False))
        approval_id: UUID | None = None
        if is_valid:
            approval = Approval.create(
                organization_id=actor.organization_id,
                proposal_id=proposal_id,
                proposal_version_number=version_number,
            )
            approval_id = approval.id
            self._session.add(
                ApprovalModel(
                    id=approval.id,
                    organization_id=approval.organization_id,
                    proposal_id=approval.proposal_id,
                    proposal_version_number=approval.proposal_version_number,
                    status=approval.status.value,
                    version=approval.version,
                    created_at=approval.created_at,
                    updated_at=approval.updated_at,
                )
            )
            proposal_model.status = ProposalStatus.READY_FOR_DECISION.value
            proposal_model.approval_id = approval.id
            proposal_model.version += 1
            proposal_model.updated_at = datetime.now(UTC)
        self._session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=actor.organization_id,
                actor_membership_id=actor.membership_id,
                action="proposal.validated",
                outcome=AuditOutcome.SUCCEEDED,
                resource_type="proposal",
                resource_id=proposal_id,
                request_id=request_id,
                idempotency_key=None,
                before_data={},
                after_data={
                    "version": version_number,
                    "can_approve": is_valid,
                    "approval_id": str(approval_id) if approval_id else None,
                },
                reason_data={},
            )
        )
        await self._session.flush()
        return Proposal(
            id=proposal_model.id,
            organization_id=proposal_model.organization_id,
            workflow_run_id=proposal_model.workflow_run_id,
            status=ProposalStatus(proposal_model.status),
            current_version_number=proposal_model.current_version_number,
            approval_id=proposal_model.approval_id,
            superseded_approval_id=proposal_model.superseded_approval_id,
            version=proposal_model.version,
            created_at=proposal_model.created_at,
            updated_at=proposal_model.updated_at,
        )

    async def edit_proposal_mutation(self, **values: object) -> ProposalMutationResult:
        actor = values["actor"]
        proposal = values["proposal"]
        version = values["version"]
        approval = values["superseded_approval"]
        job = values["job"]
        assert isinstance(actor, AuthenticatedActor)
        assert isinstance(proposal, Proposal)
        assert isinstance(version, ProposalVersion)
        assert approval is None or isinstance(approval, Approval)
        assert isinstance(job, WorkflowJob)
        request_id = str(values["request_id"])
        idempotency_key = str(values["idempotency_key"])
        request_fingerprint = str(values["request_fingerprint"])
        operation = f"proposal.edit:{proposal.id}"
        replay = await self._find_replay(
            actor=actor,
            operation=operation,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            saved_proposal = await self.get_proposal(actor=actor, proposal_id=proposal.id)
            saved_version = await self.get_proposal_version(
                actor=actor,
                proposal_id=proposal.id,
                version_number=int(replay["version"]),
            )
            if saved_proposal is None or saved_version is None:
                raise PlanningRunDomainError("idempotency replay resource is unavailable")
            return ProposalMutationResult(
                proposal=saved_proposal, version=saved_version, replayed=True
            )
        locked = await self._session.scalar(
            select(ProposalModel)
            .where(
                ProposalModel.organization_id == actor.organization_id,
                ProposalModel.id == proposal.id,
                ProposalModel.current_version_number == version.version_number - 1,
                ProposalModel.version == proposal.version - 1,
            )
            .with_for_update()
        )
        if locked is None:
            raise InvalidTransitionError("proposal changed concurrently")
        now = datetime.now(UTC)
        record = self._new_idempotency(
            actor=actor,
            operation=operation,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            now=now,
        )
        if approval is not None:
            approval_result = await self._session.execute(
                update(ApprovalModel)
                .where(
                    ApprovalModel.organization_id == actor.organization_id,
                    ApprovalModel.id == approval.id,
                    ApprovalModel.status == ApprovalStatus.PENDING.value,
                    ApprovalModel.version == approval.version - 1,
                )
                .values(
                    status=approval.status.value,
                    version=approval.version,
                    updated_at=approval.updated_at,
                )
            )
            assert isinstance(approval_result, CursorResult)
            if approval_result.rowcount != 1:
                raise InvalidTransitionError("approval changed concurrently")
        self._session.add(
            ProposalVersionModel(
                id=version.id,
                organization_id=version.organization_id,
                proposal_id=version.proposal_id,
                version_number=version.version_number,
                created_by_membership_id=version.created_by_membership_id,
                content=version.content,
                assumptions=version.assumptions,
                change_summary=version.change_summary,
                field_provenance=version.field_provenance,
                validation_result=version.validation_result,
                source_reference_snapshot=version.source_reference_snapshot,
                workflow_version=version.workflow_version,
                prompt_version=version.prompt_version,
                schema_version=version.schema_version,
                model_reference=version.model_reference,
                verifier_version=version.verifier_version,
                creator_type=version.creator_type,
                created_at=version.created_at,
            )
        )
        locked.status = proposal.status.value
        locked.current_version_number = proposal.current_version_number
        locked.approval_id = None
        locked.superseded_approval_id = proposal.superseded_approval_id
        locked.version = proposal.version
        locked.updated_at = proposal.updated_at
        self._session.add(
            WorkflowJobModel(
                id=job.id,
                organization_id=job.organization_id,
                workflow_run_id=job.workflow_run_id,
                job_type=job.job_type,
                status=job.status.value,
                payload=job.payload,
                attempt_count=job.attempt_count,
                max_attempts=job.max_attempts,
                available_at=job.available_at,
                created_at=job.created_at,
                updated_at=job.updated_at,
            )
        )
        self._audit_success(
            actor=actor,
            action="proposal.edited",
            resource_type="proposal",
            resource_id=proposal.id,
            request_id=request_id,
            idempotency_key=idempotency_key,
            after_data={"version": version.version_number, "status": proposal.status.value},
        )
        record.state = IdempotencyState.COMPLETED
        record.response_status = 202
        record.response_body = {
            "proposal_id": str(proposal.id),
            "version": version.version_number,
        }
        await self._session.flush()
        return ProposalMutationResult(proposal=proposal, version=version, replayed=False)

    async def find_proposal_mutation_replay(
        self, **values: object
    ) -> ProposalMutationResult | None:
        actor = values["actor"]
        assert isinstance(actor, AuthenticatedActor)
        replay = await self._find_replay(
            actor=actor,
            operation=str(values["operation"]),
            idempotency_key=str(values["idempotency_key"]),
            request_fingerprint=str(values["request_fingerprint"]),
        )
        if replay is None:
            return None
        proposal_id = UUID(str(replay["proposal_id"]))
        version_number = int(replay["version"])
        proposal = await self.get_proposal(actor=actor, proposal_id=proposal_id)
        version = await self.get_proposal_version(
            actor=actor,
            proposal_id=proposal_id,
            version_number=version_number,
        )
        if proposal is None or version is None:
            raise PlanningRunDomainError("idempotency replay resource is unavailable")
        return ProposalMutationResult(proposal=proposal, version=version, replayed=True)

    async def request_ai_revision_mutation(self, **values: object) -> ProposalRevisionRequestResult:
        actor = values["actor"]
        proposal_id = values["proposal_id"]
        job = values["job"]
        assert isinstance(actor, AuthenticatedActor)
        assert isinstance(proposal_id, UUID)
        assert isinstance(job, WorkflowJob)
        expected_version_value = values["expected_version"]
        assert isinstance(expected_version_value, int)
        expected_version = expected_version_value
        request_id = str(values["request_id"])
        idempotency_key = str(values["idempotency_key"])
        request_fingerprint = str(values["request_fingerprint"])
        operation = f"proposal.ai_revise:{proposal_id}"
        replay = await self._find_replay(
            actor=actor,
            operation=operation,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            return ProposalRevisionRequestResult(
                proposal_id=UUID(str(replay["proposal_id"])),
                base_version=int(replay["base_version"]),
                workflow_run_id=UUID(str(replay["workflow_run_id"])),
                revision_job_id=UUID(str(replay["revision_job_id"])),
                replayed=True,
            )
        proposal = await self._session.scalar(
            select(ProposalModel)
            .where(
                ProposalModel.organization_id == actor.organization_id,
                ProposalModel.id == proposal_id,
            )
            .with_for_update()
        )
        if proposal is None:
            raise PlanningRunNotFoundError
        if proposal.current_version_number != expected_version:
            raise ResourceVersionMismatchError(proposal.current_version_number)
        if (
            proposal.status != ProposalStatus.READY_FOR_DECISION.value
            or proposal.approval_id is None
        ):
            raise ApprovalStateConflictError
        approval = await self._session.scalar(
            select(ApprovalModel)
            .where(
                ApprovalModel.organization_id == actor.organization_id,
                ApprovalModel.id == proposal.approval_id,
                ApprovalModel.proposal_id == proposal.id,
                ApprovalModel.proposal_version_number == expected_version,
                ApprovalModel.status == ApprovalStatus.PENDING.value,
            )
            .with_for_update()
        )
        version = await self._session.scalar(
            select(ProposalVersionModel).where(
                ProposalVersionModel.organization_id == actor.organization_id,
                ProposalVersionModel.proposal_id == proposal.id,
                ProposalVersionModel.version_number == expected_version,
            )
        )
        if approval is None or version is None:
            raise ApprovalStateConflictError
        now = datetime.now(UTC)
        record = self._new_idempotency(
            actor=actor,
            operation=operation,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            now=now,
        )
        self._session.add(
            WorkflowJobModel(
                id=job.id,
                organization_id=actor.organization_id,
                workflow_run_id=proposal.workflow_run_id,
                job_type=job.job_type,
                status=job.status.value,
                payload=job.payload,
                attempt_count=job.attempt_count,
                max_attempts=job.max_attempts,
                available_at=job.available_at,
                created_at=job.created_at,
                updated_at=job.updated_at,
            )
        )
        body: dict[str, object] = {
            "proposal_id": str(proposal.id),
            "base_version": expected_version,
            "workflow_run_id": str(proposal.workflow_run_id),
            "revision_job_id": str(job.id),
        }
        self._audit_success(
            actor=actor,
            action="proposal.ai_revision_requested",
            resource_type="proposal",
            resource_id=proposal.id,
            request_id=request_id,
            idempotency_key=idempotency_key,
            after_data=body,
        )
        record.state = IdempotencyState.COMPLETED
        record.response_status = 202
        record.response_body = body
        await self._session.flush()
        return ProposalRevisionRequestResult(
            proposal_id=proposal.id,
            base_version=expected_version,
            workflow_run_id=proposal.workflow_run_id,
            revision_job_id=job.id,
            replayed=False,
        )

    async def get_ai_revision_preflight(
        self, **values: object
    ) -> ProposalAIRevisionPreflight | None:
        actor = values["actor"]
        proposal_id = values["proposal_id"]
        assert isinstance(actor, AuthenticatedActor)
        assert isinstance(proposal_id, UUID)
        base_version_value = values["base_version"]
        assert isinstance(base_version_value, int)
        base_version = base_version_value
        proposal = await self.get_proposal(actor=actor, proposal_id=proposal_id)
        if (
            proposal is None
            or proposal.status is not ProposalStatus.READY_FOR_DECISION
            or proposal.current_version_number != base_version
            or proposal.approval_id is None
        ):
            return None
        version = await self.get_proposal_version(
            actor=actor,
            proposal_id=proposal_id,
            version_number=base_version,
        )
        approval = await self.get_approval(actor=actor, approval_id=proposal.approval_id)
        run = await self.get_workflow_run(actor=actor, run_id=proposal.workflow_run_id)
        if (
            version is None
            or approval is None
            or run is None
            or approval.status is not ApprovalStatus.PENDING
            or approval.proposal_version_number != base_version
        ):
            return None
        try:
            await self._verify_source_freshness(
                actor=actor,
                snapshot=version.source_reference_snapshot,
            )
        except ProposalStaleError:
            return None
        checkpoint = await self.get_latest_checkpoint(actor=actor, run_id=run.id)
        locale = "en"
        if checkpoint is not None and checkpoint.state.get("locale") in {"vi", "en"}:
            locale = str(checkpoint.state["locale"])
        return ProposalAIRevisionPreflight(
            run=run,
            proposal=proposal,
            version=version,
            approval=approval,
            locale=locale,
        )

    async def finalize_ai_revision_mutation(self, **values: object) -> ProposalVersion | None:
        actor = values["actor"]
        proposal_id = values["proposal_id"]
        content = values["content"]
        validation_result = values["validation_result"]
        assert isinstance(actor, AuthenticatedActor)
        assert isinstance(proposal_id, UUID)
        assert isinstance(content, dict)
        assert isinstance(validation_result, dict)
        typed_content = cast(dict[str, object], content)
        typed_validation = cast(dict[str, object], validation_result)
        base_version_value = values["base_version"]
        assert isinstance(base_version_value, int)
        base_version = base_version_value
        if actor.role not in {MembershipRole.ADMIN, MembershipRole.MANAGER}:
            return None
        proposal = await self._session.scalar(
            select(ProposalModel)
            .where(
                ProposalModel.organization_id == actor.organization_id,
                ProposalModel.id == proposal_id,
                ProposalModel.current_version_number == base_version,
                ProposalModel.status == ProposalStatus.READY_FOR_DECISION.value,
            )
            .with_for_update()
        )
        if proposal is None or proposal.approval_id is None:
            return None
        base = await self._session.scalar(
            select(ProposalVersionModel).where(
                ProposalVersionModel.organization_id == actor.organization_id,
                ProposalVersionModel.proposal_id == proposal_id,
                ProposalVersionModel.version_number == base_version,
            )
        )
        approval = await self._session.scalar(
            select(ApprovalModel)
            .where(
                ApprovalModel.organization_id == actor.organization_id,
                ApprovalModel.id == proposal.approval_id,
                ApprovalModel.proposal_id == proposal_id,
                ApprovalModel.proposal_version_number == base_version,
                ApprovalModel.status == ApprovalStatus.PENDING.value,
            )
            .with_for_update()
        )
        if base is None or approval is None:
            return None
        try:
            await self._verify_source_freshness(
                actor=actor,
                snapshot=base.source_reference_snapshot,
            )
        except ProposalStaleError:
            return None
        if any(
            task.get("assignee_membership_id") is not None
            for task in _items(typed_content.get("tasks"), "tasks")
        ):
            return None
        new_version = ProposalVersion(
            id=uuid4(),
            organization_id=actor.organization_id,
            proposal_id=proposal_id,
            version_number=base_version + 1,
            created_by_membership_id=actor.membership_id,
            content=cast(dict[str, Any], typed_content),
            assumptions=list(base.assumptions),
            change_summary=str(values["change_summary"])[:4_000],
            field_provenance={"default": "AI_PROPOSED"},
            validation_result=cast(dict[str, Any], typed_validation),
            source_reference_snapshot=list(base.source_reference_snapshot),
            workflow_version=base.workflow_version,
            prompt_version=base.prompt_version,
            schema_version=base.schema_version,
            model_reference=str(values["model_reference"])[:255],
            verifier_version=base.verifier_version,
            creator_type="AI_SYSTEM",
        )
        await self.append_proposal_version(version=new_version)
        now = datetime.now(UTC)
        approval.status = ApprovalStatus.SUPERSEDED.value
        approval.version += 1
        approval.updated_at = now
        proposal.status = ProposalStatus.DRAFT.value
        proposal.current_version_number = new_version.version_number
        proposal.superseded_approval_id = proposal.approval_id
        proposal.approval_id = None
        proposal.version += 1
        proposal.updated_at = now
        revalidate = WorkflowJob(
            id=uuid4(),
            organization_id=actor.organization_id,
            workflow_run_id=proposal.workflow_run_id,
            job_type="proposal.revalidate",
            status=WorkflowJobStatus.QUEUED,
            payload={
                "instruction": "REVALIDATE",
                "proposal_id": str(proposal_id),
                "proposal_version": new_version.version_number,
            },
        )
        self._session.add(
            WorkflowJobModel(
                id=revalidate.id,
                organization_id=revalidate.organization_id,
                workflow_run_id=revalidate.workflow_run_id,
                job_type=revalidate.job_type,
                status=revalidate.status.value,
                payload=revalidate.payload,
                attempt_count=0,
                max_attempts=revalidate.max_attempts,
                available_at=revalidate.available_at,
                created_at=revalidate.created_at,
                updated_at=revalidate.updated_at,
            )
        )
        self._audit_success(
            actor=actor,
            action="proposal.ai_revised",
            resource_type="proposal",
            resource_id=proposal_id,
            request_id=str(values["request_id"]),
            idempotency_key=str(values["idempotency_key"]),
            after_data={
                "base_version": base_version,
                "version": new_version.version_number,
                "revalidation_job_id": str(revalidate.id),
            },
        )
        await self.append_event(
            actor=actor,
            run_id=proposal.workflow_run_id,
            event_type="proposal.superseded",
            public_payload={
                "proposal_id": str(proposal_id),
                "base_version": base_version,
                "current_version": new_version.version_number,
            },
        )
        await self._session.flush()
        return new_version

    async def update_workflow_run(
        self,
        *,
        actor: AuthenticatedActor,
        run: WorkflowRun,
    ) -> WorkflowRun:
        stmt = (
            update(WorkflowRunModel)
            .where(
                WorkflowRunModel.id == run.id,
                WorkflowRunModel.organization_id == actor.organization_id,
                WorkflowRunModel.version == run.version - 1,
            )
            .values(
                status=run.status.value,
                error_message=run.error_message,
                version=run.version,
                updated_at=run.updated_at,
            )
        )
        result = await self._session.execute(stmt)
        assert isinstance(result, CursorResult)
        if result.rowcount == 0:
            raise RuntimeError("WorkflowRun update failed: concurrent mutation or not found.")
        return run

    async def save_checkpoint(
        self,
        *,
        checkpoint: WorkflowCheckpoint,
    ) -> WorkflowCheckpoint:
        stmt = (
            select(WorkflowCheckpointModel)
            .where(
                WorkflowCheckpointModel.organization_id == checkpoint.organization_id,
                WorkflowCheckpointModel.workflow_run_id == checkpoint.workflow_run_id,
                WorkflowCheckpointModel.sequence == checkpoint.sequence,
            )
            .with_for_update()
        )
        result = await self._session.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing is not None:
            if existing.node == checkpoint.node and existing.state == checkpoint.state:
                return checkpoint
            raise InvalidTransitionError(
                "Checkpoint conflict: sequence "
                f"{checkpoint.sequence} already exists "
                "with different node or state."
            )

        model = WorkflowCheckpointModel(
            id=checkpoint.id,
            organization_id=checkpoint.organization_id,
            workflow_run_id=checkpoint.workflow_run_id,
            node=checkpoint.node,
            sequence=checkpoint.sequence,
            state=checkpoint.state,
            created_at=checkpoint.created_at,
        )
        self._session.add(model)
        await self._session.flush()
        return checkpoint

    async def get_latest_checkpoint(
        self,
        *,
        actor: AuthenticatedActor,
        run_id: UUID,
    ) -> WorkflowCheckpoint | None:
        stmt = (
            select(WorkflowCheckpointModel)
            .where(
                WorkflowCheckpointModel.workflow_run_id == run_id,
                WorkflowCheckpointModel.organization_id == actor.organization_id,
            )
            .order_by(WorkflowCheckpointModel.sequence.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return WorkflowCheckpoint(
            id=model.id,
            organization_id=model.organization_id,
            workflow_run_id=model.workflow_run_id,
            node=model.node,
            sequence=model.sequence,
            state=model.state,
            created_at=model.created_at,
        )

    async def create_proposal(
        self,
        *,
        proposal: Proposal,
        initial_version: ProposalVersion,
    ) -> Proposal:
        prop_model = ProposalModel(
            id=proposal.id,
            organization_id=proposal.organization_id,
            workflow_run_id=proposal.workflow_run_id,
            status=proposal.status.value,
            current_version_number=proposal.current_version_number,
            approval_id=proposal.approval_id,
            superseded_approval_id=proposal.superseded_approval_id,
            version=proposal.version,
            created_at=proposal.created_at,
            updated_at=proposal.updated_at,
        )
        ver_model = ProposalVersionModel(
            id=initial_version.id,
            organization_id=initial_version.organization_id,
            proposal_id=initial_version.proposal_id,
            version_number=initial_version.version_number,
            created_by_membership_id=initial_version.created_by_membership_id,
            content=initial_version.content,
            assumptions=initial_version.assumptions,
            change_summary=initial_version.change_summary,
            field_provenance=initial_version.field_provenance,
            validation_result=initial_version.validation_result,
            source_reference_snapshot=initial_version.source_reference_snapshot,
            workflow_version=initial_version.workflow_version,
            prompt_version=initial_version.prompt_version,
            schema_version=initial_version.schema_version,
            model_reference=initial_version.model_reference,
            verifier_version=initial_version.verifier_version,
            creator_type=initial_version.creator_type,
            created_at=initial_version.created_at,
        )
        self._session.add(prop_model)
        self._session.add(ver_model)
        await self._session.flush()
        return proposal

    async def get_proposal(
        self,
        *,
        actor: AuthenticatedActor,
        proposal_id: UUID,
    ) -> Proposal | None:
        stmt = select(ProposalModel).where(
            ProposalModel.id == proposal_id,
            ProposalModel.organization_id == actor.organization_id,
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return Proposal(
            id=model.id,
            organization_id=model.organization_id,
            workflow_run_id=model.workflow_run_id,
            status=ProposalStatus(model.status),
            current_version_number=model.current_version_number,
            approval_id=model.approval_id,
            superseded_approval_id=model.superseded_approval_id,
            version=model.version,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    async def get_proposal_by_run_id(
        self,
        *,
        actor: AuthenticatedActor,
        run_id: UUID,
    ) -> Proposal | None:
        stmt = select(ProposalModel).where(
            ProposalModel.workflow_run_id == run_id,
            ProposalModel.organization_id == actor.organization_id,
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return Proposal(
            id=model.id,
            organization_id=model.organization_id,
            workflow_run_id=model.workflow_run_id,
            status=ProposalStatus(model.status),
            current_version_number=model.current_version_number,
            approval_id=model.approval_id,
            superseded_approval_id=model.superseded_approval_id,
            version=model.version,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    async def update_proposal(
        self,
        *,
        actor: AuthenticatedActor,
        proposal: Proposal,
    ) -> Proposal:
        stmt = (
            update(ProposalModel)
            .where(
                ProposalModel.id == proposal.id,
                ProposalModel.organization_id == actor.organization_id,
                ProposalModel.version == proposal.version - 1,
                ProposalModel.current_version_number == proposal.current_version_number,
            )
            .values(
                status=proposal.status.value,
                current_version_number=proposal.current_version_number,
                approval_id=proposal.approval_id,
                superseded_approval_id=proposal.superseded_approval_id,
                version=proposal.version,
                updated_at=proposal.updated_at,
            )
        )
        result = await self._session.execute(stmt)
        assert isinstance(result, CursorResult)
        if result.rowcount == 0:
            raise RuntimeError("Proposal update failed: concurrent mutation or not found.")
        return proposal

    async def edit_proposal(
        self,
        *,
        actor: AuthenticatedActor,
        proposal: Proposal,
        version: ProposalVersion,
        superseded_approval: Approval,
    ) -> Proposal:
        if actor.role != MembershipRole.MANAGER:
            raise PermissionError("Proposal edits require the Manager role.")
        if version.created_by_membership_id != actor.membership_id:
            raise PermissionError("Proposal version must identify the authenticated actor.")
        if (
            proposal.status != ProposalStatus.DRAFT
            or proposal.organization_id != actor.organization_id
            or proposal.id != version.proposal_id
            or proposal.id != superseded_approval.proposal_id
            or proposal.organization_id != version.organization_id
            or proposal.organization_id != superseded_approval.organization_id
            or proposal.current_version_number != version.version_number
            or proposal.superseded_approval_id != superseded_approval.id
            or proposal.approval_id is not None
            or superseded_approval.status != ApprovalStatus.SUPERSEDED
            or superseded_approval.proposal_version_number != version.version_number - 1
        ):
            raise RuntimeError("Proposal edit, version, and superseded Approval do not match.")

        locked_proposal = await self._session.scalar(
            select(ProposalModel)
            .where(
                ProposalModel.id == proposal.id,
                ProposalModel.organization_id == actor.organization_id,
                ProposalModel.status == ProposalStatus.READY_FOR_DECISION.value,
                ProposalModel.approval_id == superseded_approval.id,
                ProposalModel.current_version_number == version.version_number - 1,
                ProposalModel.version == proposal.version - 1,
            )
            .with_for_update()
        )
        if locked_proposal is None:
            raise RuntimeError("Proposal edit failed: concurrent mutation or approval superseded.")

        approval_result = await self._session.execute(
            update(ApprovalModel)
            .where(
                ApprovalModel.id == superseded_approval.id,
                ApprovalModel.organization_id == actor.organization_id,
                ApprovalModel.proposal_id == proposal.id,
                ApprovalModel.status == ApprovalStatus.PENDING.value,
                ApprovalModel.version == superseded_approval.version - 1,
            )
            .values(
                status=superseded_approval.status.value,
                version=superseded_approval.version,
                updated_at=superseded_approval.updated_at,
            )
        )
        assert isinstance(approval_result, CursorResult)
        if approval_result.rowcount == 0:
            raise RuntimeError("Proposal edit failed: Approval concurrency failure.")

        self._session.add(
            ProposalVersionModel(
                id=version.id,
                organization_id=version.organization_id,
                proposal_id=version.proposal_id,
                version_number=version.version_number,
                created_by_membership_id=version.created_by_membership_id,
                content=version.content,
                assumptions=version.assumptions,
                change_summary=version.change_summary,
                field_provenance=version.field_provenance,
                validation_result=version.validation_result,
                source_reference_snapshot=version.source_reference_snapshot,
                workflow_version=version.workflow_version,
                prompt_version=version.prompt_version,
                schema_version=version.schema_version,
                model_reference=version.model_reference,
                verifier_version=version.verifier_version,
                creator_type=version.creator_type,
                created_at=version.created_at,
            )
        )
        await self._session.flush()

        proposal_result = await self._session.execute(
            update(ProposalModel)
            .where(
                ProposalModel.id == proposal.id,
                ProposalModel.organization_id == actor.organization_id,
                ProposalModel.status == ProposalStatus.READY_FOR_DECISION.value,
                ProposalModel.approval_id == superseded_approval.id,
                ProposalModel.current_version_number == version.version_number - 1,
                ProposalModel.version == proposal.version - 1,
            )
            .values(
                status=proposal.status.value,
                current_version_number=proposal.current_version_number,
                approval_id=None,
                superseded_approval_id=superseded_approval.id,
                version=proposal.version,
                updated_at=proposal.updated_at,
            )
        )
        assert isinstance(proposal_result, CursorResult)
        if proposal_result.rowcount == 0:
            raise RuntimeError("Proposal edit failed: concurrent mutation or not found.")
        return proposal

    async def append_proposal_version(
        self,
        *,
        version: ProposalVersion,
    ) -> ProposalVersion:
        model = ProposalVersionModel(
            id=version.id,
            organization_id=version.organization_id,
            proposal_id=version.proposal_id,
            version_number=version.version_number,
            created_by_membership_id=version.created_by_membership_id,
            content=version.content,
            assumptions=version.assumptions,
            change_summary=version.change_summary,
            field_provenance=version.field_provenance,
            validation_result=version.validation_result,
            source_reference_snapshot=version.source_reference_snapshot,
            workflow_version=version.workflow_version,
            prompt_version=version.prompt_version,
            schema_version=version.schema_version,
            model_reference=version.model_reference,
            verifier_version=version.verifier_version,
            creator_type=version.creator_type,
            created_at=version.created_at,
        )
        self._session.add(model)
        await self._session.flush()
        return version

    async def get_proposal_version(
        self,
        *,
        actor: AuthenticatedActor,
        proposal_id: UUID,
        version_number: int,
    ) -> ProposalVersion | None:
        stmt = select(ProposalVersionModel).where(
            ProposalVersionModel.proposal_id == proposal_id,
            ProposalVersionModel.version_number == version_number,
            ProposalVersionModel.organization_id == actor.organization_id,
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return ProposalVersion(
            id=model.id,
            organization_id=model.organization_id,
            proposal_id=model.proposal_id,
            version_number=model.version_number,
            created_by_membership_id=model.created_by_membership_id,
            content=model.content,
            assumptions=model.assumptions,
            change_summary=model.change_summary,
            field_provenance=model.field_provenance,
            validation_result=model.validation_result,
            source_reference_snapshot=model.source_reference_snapshot,
            workflow_version=model.workflow_version,
            prompt_version=model.prompt_version,
            schema_version=model.schema_version,
            model_reference=model.model_reference,
            verifier_version=model.verifier_version,
            creator_type=model.creator_type,
            created_at=model.created_at,
        )

    async def get_proposal_version_creator_by_scope(
        self,
        *,
        organization_id: UUID,
        proposal_id: UUID,
        version_number: int,
    ) -> UUID | None:
        return await self._session.scalar(
            select(ProposalVersionModel.created_by_membership_id).where(
                ProposalVersionModel.organization_id == organization_id,
                ProposalVersionModel.proposal_id == proposal_id,
                ProposalVersionModel.version_number == version_number,
            )
        )

    async def create_approval(
        self,
        *,
        approval: Approval,
    ) -> Approval:
        model = ApprovalModel(
            id=approval.id,
            organization_id=approval.organization_id,
            proposal_id=approval.proposal_id,
            proposal_version_number=approval.proposal_version_number,
            status=approval.status.value,
            decided_by_membership_id=approval.decided_by_membership_id,
            decision_reason=approval.decision_reason,
            decided_at=approval.decided_at,
            version=approval.version,
            created_at=approval.created_at,
            updated_at=approval.updated_at,
        )
        self._session.add(model)
        await self._session.flush()
        return approval

    async def get_approval(
        self,
        *,
        actor: AuthenticatedActor,
        approval_id: UUID,
    ) -> Approval | None:
        stmt = select(ApprovalModel).where(
            ApprovalModel.id == approval_id,
            ApprovalModel.organization_id == actor.organization_id,
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return Approval(
            id=model.id,
            organization_id=model.organization_id,
            proposal_id=model.proposal_id,
            proposal_version_number=model.proposal_version_number,
            status=ApprovalStatus(model.status),
            decided_by_membership_id=model.decided_by_membership_id,
            decision_reason=model.decision_reason,
            decided_at=model.decided_at,
            version=model.version,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    async def get_approval_decider_by_scope(
        self, *, organization_id: UUID, approval_id: UUID
    ) -> UUID | None:
        return await self._session.scalar(
            select(ApprovalModel.decided_by_membership_id).where(
                ApprovalModel.organization_id == organization_id,
                ApprovalModel.id == approval_id,
                ApprovalModel.status.in_(
                    (ApprovalStatus.APPROVED.value, ApprovalStatus.REJECTED.value)
                ),
            )
        )

    async def decide_approval(
        self,
        *,
        actor: AuthenticatedActor,
        approval: Approval,
        proposal: Proposal,
    ) -> Approval:
        if actor.role not in {MembershipRole.ADMIN, MembershipRole.MANAGER}:
            raise PermissionError("Approval decisions require an authorized management role.")
        if approval.decided_by_membership_id != actor.membership_id:
            raise PermissionError("Approval decision must identify the authenticated actor.")
        expected_proposal_status = {
            ApprovalStatus.APPROVED: ProposalStatus.APPROVED,
            ApprovalStatus.REJECTED: ProposalStatus.REJECTED,
        }.get(approval.status)
        if expected_proposal_status is None or proposal.status != expected_proposal_status:
            raise RuntimeError("Approval decision and Proposal terminal status do not match.")
        if (
            proposal.id != approval.proposal_id
            or proposal.organization_id != approval.organization_id
            or proposal.organization_id != actor.organization_id
            or proposal.approval_id != approval.id
            or proposal.current_version_number != approval.proposal_version_number
        ):
            raise RuntimeError("Approval decision and Proposal identity or version do not match.")

        prop_stmt = (
            select(ProposalModel)
            .where(
                ProposalModel.id == approval.proposal_id,
                ProposalModel.organization_id == actor.organization_id,
                ProposalModel.status == ProposalStatus.READY_FOR_DECISION.value,
                ProposalModel.approval_id == approval.id,
                ProposalModel.current_version_number == approval.proposal_version_number,
                ProposalModel.version == proposal.version - 1,
            )
            .with_for_update()
        )
        prop_res = await self._session.execute(prop_stmt)
        if prop_res.scalar_one_or_none() is None:
            raise RuntimeError(
                "Approval update failed: proposal is not READY_FOR_DECISION, "
                "version mismatched, or approval superseded."
            )

        stmt = (
            update(ApprovalModel)
            .where(
                ApprovalModel.id == approval.id,
                ApprovalModel.organization_id == actor.organization_id,
                ApprovalModel.status == ApprovalStatus.PENDING.value,
                ApprovalModel.version == approval.version - 1,
            )
            .values(
                status=approval.status.value,
                decided_by_membership_id=approval.decided_by_membership_id,
                decision_reason=approval.decision_reason,
                decided_at=approval.decided_at,
                version=approval.version,
                updated_at=approval.updated_at,
            )
        )
        result = await self._session.execute(stmt)
        assert isinstance(result, CursorResult)
        if result.rowcount == 0:
            raise RuntimeError(
                "Approval update failed: optimistic concurrency failure, not found, or unpermitted."
            )

        proposal_stmt = (
            update(ProposalModel)
            .where(
                ProposalModel.id == proposal.id,
                ProposalModel.organization_id == actor.organization_id,
                ProposalModel.version == proposal.version - 1,
                ProposalModel.status == ProposalStatus.READY_FOR_DECISION.value,
                ProposalModel.approval_id == approval.id,
            )
            .values(
                status=proposal.status.value,
                version=proposal.version,
                updated_at=proposal.updated_at,
            )
        )
        proposal_result = await self._session.execute(proposal_stmt)
        assert isinstance(proposal_result, CursorResult)
        if proposal_result.rowcount == 0:
            raise RuntimeError("Proposal decision failed: concurrent mutation or not found.")
        return approval

    async def append_event(
        self,
        *,
        event: WorkflowEvent | None = None,
        actor: AuthenticatedActor | None = None,
        run_id: UUID | None = None,
        event_type: str | None = None,
        public_payload: dict[str, Any] | None = None,
    ) -> WorkflowEvent:
        if event is not None:
            org_id = event.organization_id
            target_run_id = event.workflow_run_id
            target_event_type = event.event_type
            payload = event.public_payload
            event_id = event.id
            created_at = event.created_at
        else:
            if actor is None or run_id is None or event_type is None or public_payload is None:
                raise PlanningRunDomainError(
                    "append_event requires either event or "
                    "(actor, run_id, event_type, "
                    "public_payload)."
                )
            org_id = actor.organization_id
            target_run_id = run_id
            target_event_type = event_type
            payload = public_payload
            event_id = uuid4()
            created_at = datetime.now(UTC)

        run_stmt = (
            select(WorkflowRunModel)
            .where(
                WorkflowRunModel.id == target_run_id,
                WorkflowRunModel.organization_id == org_id,
            )
            .with_for_update()
        )
        run_res = await self._session.execute(run_stmt)
        if run_res.scalar_one_or_none() is None:
            raise PlanningRunDomainError("WorkflowRun not found or tenant mismatch.")

        # Repository always owns sequence computation
        seq_stmt = select(
            func.coalesce(
                func.max(WorkflowEventModel.sequence),
                0,
            ),
        ).where(
            WorkflowEventModel.organization_id == org_id,
            WorkflowEventModel.workflow_run_id == target_run_id,
        )
        max_seq = (await self._session.execute(seq_stmt)).scalar_one()
        computed_seq = max_seq + 1

        model = WorkflowEventModel(
            id=event_id,
            organization_id=org_id,
            workflow_run_id=target_run_id,
            sequence=computed_seq,
            event_type=target_event_type,
            public_payload=payload,
            created_at=created_at,
        )
        self._session.add(model)
        await self._session.flush()
        return WorkflowEvent(
            id=model.id,
            organization_id=model.organization_id,
            workflow_run_id=model.workflow_run_id,
            sequence=model.sequence,
            event_type=model.event_type,
            public_payload=model.public_payload,
            created_at=model.created_at,
        )

    async def list_events(
        self,
        *,
        actor: AuthenticatedActor,
        run_id: UUID,
        after_sequence: int = 0,
    ) -> list[WorkflowEvent]:
        stmt = (
            select(WorkflowEventModel)
            .where(
                WorkflowEventModel.workflow_run_id == run_id,
                WorkflowEventModel.organization_id == actor.organization_id,
                WorkflowEventModel.sequence > after_sequence,
            )
            .order_by(WorkflowEventModel.sequence.asc())
        )
        result = await self._session.execute(stmt)
        models = result.scalars().all()
        return [
            WorkflowEvent(
                id=m.id,
                organization_id=m.organization_id,
                workflow_run_id=m.workflow_run_id,
                sequence=m.sequence,
                event_type=m.event_type,
                public_payload=m.public_payload,
                created_at=m.created_at,
            )
            for m in models
        ]

    async def record_model_invocation(
        self,
        *,
        invocation: ModelInvocation,
    ) -> ModelInvocation:
        model = ModelInvocationModel(
            id=invocation.id,
            organization_id=invocation.organization_id,
            workflow_run_id=invocation.workflow_run_id,
            provider=invocation.provider,
            model_name=invocation.model_name,
            prompt_version=invocation.prompt_version,
            schema_version=invocation.schema_version,
            invocation_key=invocation.invocation_key,
            prompt_tokens=invocation.prompt_tokens,
            completion_tokens=invocation.completion_tokens,
            duration_ms=invocation.duration_ms,
            status=invocation.status,
            created_at=invocation.created_at,
        )
        self._session.add(model)
        await self._session.flush()
        return invocation

    async def add_context_reference(
        self,
        *,
        ref: ContextReference,
    ) -> ContextReference:
        model = ContextReferenceModel(
            id=ref.id,
            organization_id=ref.organization_id,
            workflow_run_id=ref.workflow_run_id,
            resource_type=ref.resource_type,
            resource_id=ref.resource_id,
            provenance_notes=ref.provenance_notes,
            created_at=ref.created_at,
        )
        self._session.add(model)
        await self._session.flush()
        return ref

    async def enqueue_outbox_event(
        self,
        *,
        event: OutboxEvent,
        organization_id: UUID,
    ) -> OutboxEvent:
        if organization_id != event.organization_id:
            raise PlanningRunDomainError("Organization ID mismatch in enqueue_outbox_event.")

        stmt = (
            select(OutboxEventModel)
            .where(
                OutboxEventModel.organization_id == event.organization_id,
                OutboxEventModel.event_id == event.event_id,
            )
            .with_for_update()
        )
        existing = (await self._session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            if (
                existing.event_type == event.event_type
                and existing.aggregate_type == event.aggregate_type
                and existing.aggregate_id == event.aggregate_id
                and existing.payload == event.payload
            ):
                return OutboxEvent(
                    id=existing.id,
                    organization_id=existing.organization_id,
                    event_id=existing.event_id,
                    event_type=existing.event_type,
                    aggregate_type=existing.aggregate_type,
                    aggregate_id=existing.aggregate_id,
                    payload=existing.payload,
                    status=OutboxStatus(existing.status),
                    envelope_version=existing.envelope_version,
                    attempt_count=existing.attempt_count,
                    max_attempts=existing.max_attempts,
                    available_at=existing.available_at,
                    published_at=existing.published_at,
                    last_error_code=existing.last_error_code,
                    last_error=existing.last_error,
                    locked_by_worker_id=existing.locked_by_worker_id,
                    lease_until=existing.lease_until,
                    occurred_at=existing.occurred_at,
                    created_at=existing.created_at,
                )
            raise PlanningRunDomainError(
                f"Conflict: Outbox event {event.event_id} exists with different attributes."
            )

        model = OutboxEventModel(
            id=event.id,
            organization_id=event.organization_id,
            event_id=event.event_id,
            event_type=event.event_type,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            payload=event.payload,
            status=event.status.value,
            envelope_version=event.envelope_version,
            attempt_count=event.attempt_count,
            max_attempts=event.max_attempts,
            available_at=event.available_at,
            published_at=event.published_at,
            last_error_code=event.last_error_code,
            last_error=event.last_error,
            locked_by_worker_id=event.locked_by_worker_id,
            lease_until=event.lease_until,
            occurred_at=event.occurred_at,
            created_at=event.created_at,
        )
        self._session.add(model)
        await self._session.flush()
        return event

    async def claim_pending_outbox_events(
        self,
        *,
        organization_id: UUID,
        worker_id: str,
        limit: int,
        now: datetime,
        lease_until: datetime,
    ) -> list[OutboxEvent]:
        stmt = (
            select(OutboxEventModel)
            .where(
                OutboxEventModel.organization_id == organization_id,
                OutboxEventModel.attempt_count < OutboxEventModel.max_attempts,
                (
                    (OutboxEventModel.status == OutboxStatus.PENDING.value)
                    & (OutboxEventModel.available_at <= now)
                )
                | (
                    (OutboxEventModel.status == OutboxStatus.DISPATCHING.value)
                    & (
                        OutboxEventModel.lease_until.is_(None)
                        | (OutboxEventModel.lease_until < now)
                    )
                ),
            )
            .order_by(OutboxEventModel.created_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self._session.execute(stmt)
        models = result.scalars().all()
        claimed: list[OutboxEvent] = []
        for model in models:
            model.status = OutboxStatus.DISPATCHING.value
            model.locked_by_worker_id = worker_id
            model.lease_until = lease_until
            model.attempt_count += 1
            claimed.append(
                OutboxEvent(
                    id=model.id,
                    organization_id=model.organization_id,
                    event_id=model.event_id,
                    event_type=model.event_type,
                    aggregate_type=model.aggregate_type,
                    aggregate_id=model.aggregate_id,
                    payload=model.payload,
                    status=OutboxStatus.DISPATCHING,
                    envelope_version=model.envelope_version,
                    attempt_count=model.attempt_count,
                    max_attempts=model.max_attempts,
                    available_at=model.available_at,
                    published_at=model.published_at,
                    last_error_code=model.last_error_code,
                    last_error=model.last_error,
                    locked_by_worker_id=model.locked_by_worker_id,
                    lease_until=model.lease_until,
                    occurred_at=model.occurred_at,
                    created_at=model.created_at,
                )
            )
        await self._session.flush()
        return claimed

    async def mark_outbox_event_published(
        self,
        *,
        organization_id: UUID,
        event_id: UUID,
        worker_id: str,
        now: datetime,
        published_at: datetime,
    ) -> None:
        stmt = (
            update(OutboxEventModel)
            .where(
                OutboxEventModel.organization_id == organization_id,
                OutboxEventModel.event_id == event_id,
                OutboxEventModel.locked_by_worker_id == worker_id,
                OutboxEventModel.status == OutboxStatus.DISPATCHING.value,
                OutboxEventModel.lease_until.is_not(None),
                OutboxEventModel.lease_until >= now,
            )
            .values(
                status=OutboxStatus.DISPATCHED.value,
                published_at=published_at,
                locked_by_worker_id=None,
                lease_until=None,
            )
        )
        result = await self._session.execute(stmt)
        assert isinstance(result, CursorResult)
        if result.rowcount == 0:
            raise PlanningRunDomainError(
                "Outbox publish failed: lease expired, wrong worker, or event not DISPATCHING."
            )

    async def record_outbox_event_failure(
        self,
        *,
        organization_id: UUID,
        event_id: UUID,
        worker_id: str,
        now: datetime,
        error_code: str,
        error_message: str,
        next_available_at: datetime,
    ) -> None:
        stmt = (
            select(OutboxEventModel)
            .where(
                OutboxEventModel.organization_id == organization_id,
                OutboxEventModel.event_id == event_id,
                OutboxEventModel.locked_by_worker_id == worker_id,
                OutboxEventModel.status == OutboxStatus.DISPATCHING.value,
                OutboxEventModel.lease_until.is_not(None),
                OutboxEventModel.lease_until >= now,
            )
            .with_for_update()
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            raise PlanningRunDomainError(
                "Outbox failure record failed: "
                "lease expired, wrong worker, "
                "or event not DISPATCHING."
            )

        new_status = (
            OutboxStatus.PENDING.value
            if model.attempt_count < model.max_attempts
            else OutboxStatus.FAILED.value
        )
        model.status = new_status
        model.last_error_code = error_code
        model.last_error = error_message
        model.available_at = next_available_at
        model.locked_by_worker_id = None
        model.lease_until = None
        await self._session.flush()

    async def claim_job(
        self,
        *,
        organization_id: UUID | None = None,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> WorkflowJob | None:
        expired_conditions = [
            WorkflowJobModel.job_type.in_(
                ("proposal.risk_replan", "proposal.risk_replan_revision")
            ),
            WorkflowJobModel.status == WorkflowJobStatus.RUNNING.value,
            WorkflowJobModel.lease_until < now,
            WorkflowJobModel.attempt_count >= WorkflowJobModel.max_attempts,
        ]
        if organization_id is not None:
            expired_conditions.append(WorkflowJobModel.organization_id == organization_id)
        expired = (
            await self._session.scalars(
                select(WorkflowJobModel)
                .where(*expired_conditions)
                .order_by(WorkflowJobModel.created_at)
                .limit(10)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for abandoned in expired:
            abandoned.status = WorkflowJobStatus.FAILED.value
            abandoned.last_error = "RISK_REPLAN_LEASE_EXPIRED"
            abandoned.locked_by_worker_id = None
            abandoned.lease_until = None
            abandoned.updated_at = now
            failed = await self.fail_risk_revision_run(
                organization_id=abandoned.organization_id,
                run_id=abandoned.workflow_run_id,
            )
            if failed or abandoned.job_type == "proposal.risk_replan_revision":
                await self.append_event(
                    event=WorkflowEvent(
                        id=uuid4(),
                        organization_id=abandoned.organization_id,
                        workflow_run_id=abandoned.workflow_run_id,
                        sequence=0,
                        event_type="workflow.failed" if failed else "proposal.revision_failed",
                        public_payload={
                            "safe_error_code": "RISK_REPLAN_LEASE_EXPIRED",
                            "manual_fallback": "PROJECT_TASK_EDITOR",
                        },
                    )
                )
        await self._session.flush()
        conditions = [
            WorkflowJobModel.attempt_count < WorkflowJobModel.max_attempts,
            (WorkflowJobModel.status == WorkflowJobStatus.QUEUED.value)
            | (
                (WorkflowJobModel.status == WorkflowJobStatus.RUNNING.value)
                & (WorkflowJobModel.lease_until < now)
            ),
            WorkflowJobModel.available_at <= now,
        ]
        if organization_id is not None:
            conditions.append(WorkflowJobModel.organization_id == organization_id)

        stmt = (
            select(WorkflowJobModel)
            .where(*conditions)
            .order_by(WorkflowJobModel.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            return None

        model.status = WorkflowJobStatus.RUNNING.value
        model.locked_by_worker_id = worker_id
        if model.job_type in {"proposal.risk_replan", "proposal.risk_replan_revision"}:
            lease_until = max(lease_until, now + timedelta(seconds=150))
        model.lease_until = lease_until
        model.attempt_count += 1
        model.updated_at = now
        await self._session.flush()

        return WorkflowJob(
            id=model.id,
            organization_id=model.organization_id,
            workflow_run_id=model.workflow_run_id,
            job_type=model.job_type,
            status=WorkflowJobStatus.RUNNING,
            payload=model.payload,
            attempt_count=model.attempt_count,
            max_attempts=model.max_attempts,
            available_at=model.available_at,
            locked_by_worker_id=model.locked_by_worker_id,
            lease_until=model.lease_until,
            last_error=model.last_error,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    async def complete_job(
        self,
        *,
        job_id: UUID,
        worker_id: str,
    ) -> None:
        stmt = (
            update(WorkflowJobModel)
            .where(
                WorkflowJobModel.id == job_id,
                WorkflowJobModel.locked_by_worker_id == worker_id,
            )
            .values(
                status=WorkflowJobStatus.COMPLETED.value,
                locked_by_worker_id=None,
                lease_until=None,
                updated_at=datetime.now(UTC),
            )
        )
        await self._session.execute(stmt)

    async def fail_job(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        error_message: str,
        next_available_at: datetime,
    ) -> None:
        stmt = (
            select(WorkflowJobModel)
            .where(
                WorkflowJobModel.id == job_id,
                WorkflowJobModel.locked_by_worker_id == worker_id,
            )
            .with_for_update()
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            return

        new_status = (
            WorkflowJobStatus.QUEUED.value
            if model.attempt_count < model.max_attempts
            else WorkflowJobStatus.FAILED.value
        )
        model.status = new_status
        model.last_error = error_message
        model.available_at = next_available_at
        model.locked_by_worker_id = None
        model.lease_until = None
        model.updated_at = datetime.now(UTC)
        await self._session.flush()
