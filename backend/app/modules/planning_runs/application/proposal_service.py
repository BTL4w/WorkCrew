"""Immutable proposal editing and deterministic revalidation scheduling."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import cast
from uuid import UUID, uuid4

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.planning_runs.application.ports import (
    PlanningRuntimePort,
    PlanningRunTransaction,
    ProposalMutationResult,
    ProposalRevisionRequestResult,
    WorkflowRunMutationResult,
)
from app.modules.planning_runs.application.run_service import fingerprint
from app.modules.planning_runs.domain.models import (
    ApprovalStatus,
    PlanningRunDomainError,
    PlanningRunForbiddenError,
    PlanningRunNotFoundError,
    Proposal,
    ProposalVersion,
    ResourceVersionMismatchError,
    WorkflowJob,
    WorkflowJobStatus,
    WorkflowRun,
)
from app.modules.planning_runs.domain.risk_replanning import RiskPlanBinding, base_content

_WRITE_ROLES = frozenset({MembershipRole.ADMIN, MembershipRole.MANAGER})


class ProposalService:
    def __init__(
        self,
        *,
        transaction_factory: Callable[[AuthenticatedActor], PlanningRunTransaction],
        runtime: PlanningRuntimePort,
    ) -> None:
        self._transaction_factory = transaction_factory
        self._runtime = runtime

    async def get_proposal_version(
        self,
        *,
        actor: AuthenticatedActor,
        proposal_id: UUID,
        version_number: int,
    ) -> tuple[Proposal, ProposalVersion]:
        if actor.role not in _WRITE_ROLES:
            async with self._transaction_factory(actor) as transaction:
                await transaction.repository.audit_rejection(
                    actor=actor,
                    action="proposal.version_viewed",
                    request_id="read",
                    reason_code="FORBIDDEN",
                    resource_id=proposal_id,
                )
            raise PlanningRunForbiddenError
        async with self._transaction_factory(actor) as transaction:
            proposal = await transaction.repository.get_proposal(
                actor=actor,
                proposal_id=proposal_id,
            )
            if proposal is None:
                raise PlanningRunNotFoundError
            version = await transaction.repository.get_proposal_version(
                actor=actor,
                proposal_id=proposal_id,
                version_number=version_number,
            )
            if version is None:
                raise PlanningRunNotFoundError
        return proposal, version

    async def _audit_mutation_error(
        self,
        *,
        actor: AuthenticatedActor,
        proposal_id: UUID,
        request_id: str,
        idempotency_key: str,
        error: Exception,
    ) -> None:
        async with self._transaction_factory(actor) as transaction:
            await transaction.repository.audit_rejection(
                actor=actor,
                action="proposal.edited",
                request_id=request_id,
                reason_code=type(error).__name__,
                idempotency_key=idempotency_key,
                resource_id=proposal_id,
            )

    async def edit_proposal(
        self,
        *,
        actor: AuthenticatedActor,
        proposal_id: UUID,
        expected_version: int,
        content: dict[str, object],
        request_id: str,
        idempotency_key: str,
    ) -> ProposalMutationResult:
        if actor.role not in _WRITE_ROLES:
            async with self._transaction_factory(actor) as transaction:
                await transaction.repository.audit_rejection(
                    actor=actor,
                    action="proposal.edited",
                    request_id=request_id,
                    reason_code="FORBIDDEN",
                    resource_id=proposal_id,
                )
            raise PlanningRunForbiddenError
        try:
            normalized = self._runtime.validate_proposal_content(content)
            request_fingerprint = fingerprint(
                "proposal.edit",
                {
                    "proposal_id": str(proposal_id),
                    "expected_version": expected_version,
                    "content": normalized,
                },
            )
            async with self._transaction_factory(actor) as transaction:
                repository = transaction.repository
                replay_source = await repository.get_proposal_version(
                    actor=actor, proposal_id=proposal_id, version_number=expected_version
                )
                if replay_source is not None and "risk_replan" in replay_source.content:
                    await repository.authenticate_risk_plan_actor(actor=actor)
                replay = await repository.find_proposal_mutation_replay(
                    actor=actor,
                    operation=f"proposal.edit:{proposal_id}",
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                )
                if replay is not None:
                    return replay
                proposal = await repository.get_proposal(actor=actor, proposal_id=proposal_id)
                if proposal is None:
                    raise PlanningRunNotFoundError
                if proposal.current_version_number != expected_version:
                    raise ResourceVersionMismatchError(proposal.current_version_number)
                previous = await repository.get_proposal_version(
                    actor=actor,
                    proposal_id=proposal_id,
                    version_number=expected_version,
                )
                if previous is None:
                    raise PlanningRunNotFoundError
                if "risk_replan" in previous.content:
                    if normalized.get("risk_replan") != previous.content["risk_replan"]:
                        raise ValueError("RISK_PLAN_BINDING_IMMUTABLE")
                    await repository.verify_risk_plan_content(actor=actor, content=previous.content)
                elif "risk_replan" in normalized:
                    raise ValueError("RISK_PLAN_BINDING_IMMUTABLE")
                raw_tasks = normalized.get("tasks", [])
                if not isinstance(raw_tasks, list):
                    raise ValueError("proposal tasks are invalid")
                for raw_task in cast(list[object], raw_tasks):
                    if not isinstance(raw_task, dict):
                        raise ValueError("proposal task is invalid")
                    task_mapping = cast(dict[object, object], raw_task)
                    value = task_mapping.get("assignee_membership_id")
                    if value is not None:
                        raise ValueError("ASSIGNEE_NOT_ALLOWED_IN_PLAN")
                validation_result = self._runtime.validate_proposal_deterministically(
                    normalized,
                    active_membership_ids=frozenset(),
                )
                superseded = None
                if proposal.approval_id is not None:
                    approval = await repository.get_approval(
                        actor=actor, approval_id=proposal.approval_id
                    )
                    if approval is None or approval.status is not ApprovalStatus.PENDING:
                        raise ResourceVersionMismatchError(proposal.current_version_number)
                    superseded = approval.mark_superseded()
                edited = proposal.edit()
                version = ProposalVersion(
                    id=uuid4(),
                    organization_id=actor.organization_id,
                    proposal_id=proposal.id,
                    version_number=edited.current_version_number,
                    created_by_membership_id=actor.membership_id,
                    content=normalized,
                    assumptions=list(previous.assumptions),
                    change_summary="Manager edited proposal",
                    field_provenance={"default": "MANAGER_EDITED"},
                    validation_result=validation_result,
                    source_reference_snapshot=list(previous.source_reference_snapshot),
                    workflow_version=previous.workflow_version,
                    prompt_version=previous.prompt_version,
                    schema_version=previous.schema_version,
                    model_reference=previous.model_reference,
                    verifier_version=previous.verifier_version,
                    creator_type="HUMAN_MANAGER",
                )
                job = WorkflowJob(
                    id=uuid4(),
                    organization_id=actor.organization_id,
                    workflow_run_id=proposal.workflow_run_id,
                    job_type="proposal.revalidate",
                    status=WorkflowJobStatus.QUEUED,
                    payload={
                        "instruction": "REVALIDATE",
                        "proposal_id": str(proposal.id),
                        "proposal_version": version.version_number,
                    },
                )
                return await repository.edit_proposal_mutation(
                    actor=actor,
                    proposal=edited,
                    version=version,
                    superseded_approval=superseded,
                    job=job,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                )
        except (PlanningRunDomainError, ValueError) as error:
            await self._audit_mutation_error(
                actor=actor,
                proposal_id=proposal_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                error=error,
            )
            raise

    async def request_risk_revision(
        self,
        *,
        actor: AuthenticatedActor,
        risk_context: dict[str, object],
        instruction: str,
        locale: str,
        request_id: str,
        idempotency_key: str,
    ) -> WorkflowRunMutationResult:
        try:
            if actor.role not in _WRITE_ROLES:
                raise PlanningRunForbiddenError
            binding = RiskPlanBinding.model_validate(risk_context)
            normalized = instruction.strip()
            if not normalized or len(normalized) > 8000 or locale not in {"vi", "en"}:
                raise ValueError("RISK_REVISION_INPUT_INVALID")
            request_fingerprint = fingerprint(
                "risk-plan.revise",
                {
                    "binding": binding.model_dump(mode="json"),
                    "instruction": normalized,
                    "locale": locale,
                },
            )
            async with self._transaction_factory(actor) as tx:
                await tx.repository.authenticate_risk_plan_actor(actor=actor)
                replay = await tx.repository.find_workflow_run_mutation_replay(
                    actor=actor,
                    operation="planning_run.create",
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                )
                if replay is not None:
                    return replay
                metadata, _ = await tx.repository.load_risk_plan(actor=actor, binding=binding)
                base_content(metadata.before)
                run = WorkflowRun.create(
                    organization_id=actor.organization_id,
                    project_id=metadata.before.project_id,
                    requested_by_membership_id=actor.membership_id,
                    workflow_name="risk_weekly_replanning",
                    workflow_version="risk-replan.v1",
                    verifier_version="risk-weekly-plan.v1",
                    input_goal_text=normalized,
                )
                job = WorkflowJob(
                    id=uuid4(),
                    organization_id=actor.organization_id,
                    workflow_run_id=run.id,
                    job_type="proposal.risk_replan",
                    status=WorkflowJobStatus.QUEUED,
                    max_attempts=1,
                    payload={
                        "risk_replan": metadata.model_dump(mode="json"),
                        "instruction": normalized,
                        "locale": locale,
                        "requester_membership_id": str(actor.membership_id),
                    },
                )
                return await tx.repository.create_planning_run_mutation(
                    actor=actor,
                    run=run,
                    job=job,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                )
        except (PlanningRunDomainError, ValueError) as error:
            async with self._transaction_factory(actor) as tx:
                await tx.repository.audit_rejection(
                    actor=actor,
                    action="risk_plan.revision_requested",
                    request_id=request_id,
                    reason_code=type(error).__name__,
                    idempotency_key=idempotency_key,
                )
            raise

    async def request_ai_revision(
        self,
        *,
        actor: AuthenticatedActor,
        proposal_id: UUID,
        expected_version: int,
        instruction: str,
        request_id: str,
        idempotency_key: str,
    ) -> ProposalRevisionRequestResult:
        """Queue an exact-version AI revision without changing proposal lifecycle."""

        if actor.role not in _WRITE_ROLES:
            async with self._transaction_factory(actor) as transaction:
                await transaction.repository.audit_rejection(
                    actor=actor,
                    action="proposal.ai_revision_requested",
                    request_id=request_id,
                    reason_code="FORBIDDEN",
                    resource_id=proposal_id,
                )
            raise PlanningRunForbiddenError
        try:
            normalized = " ".join(instruction.split())
            if not normalized or len(normalized) > 8_000:
                raise ValueError("REVISION_INSTRUCTION_INVALID")
            request_fingerprint = fingerprint(
                "proposal.ai_revise",
                {
                    "proposal_id": str(proposal_id),
                    "base_version": expected_version,
                    "instruction": normalized,
                },
            )
            job = WorkflowJob(
                id=uuid4(),
                organization_id=actor.organization_id,
                workflow_run_id=uuid4(),
                job_type="proposal.ai_revise",
                status=WorkflowJobStatus.QUEUED,
                payload={
                    "proposal_id": str(proposal_id),
                    "base_version": expected_version,
                    "instruction": normalized,
                    "requester_membership_id": str(actor.membership_id),
                    "locale": "en",
                },
            )
            async with self._transaction_factory(actor) as transaction:
                previous = await transaction.repository.get_proposal_version(
                    actor=actor, proposal_id=proposal_id, version_number=expected_version
                )
                if previous is not None and "risk_replan" in previous.content:
                    await transaction.repository.verify_risk_plan_content(
                        actor=actor, content=previous.content
                    )
                    proposal = await transaction.repository.get_proposal(
                        actor=actor, proposal_id=proposal_id
                    )
                    if proposal is None:
                        raise PlanningRunNotFoundError
                    checkpoint = await transaction.repository.get_latest_checkpoint(
                        actor=actor, run_id=proposal.workflow_run_id
                    )
                    job = replace(
                        job,
                        job_type="proposal.risk_replan_revision",
                        max_attempts=1,
                        payload={
                            **job.payload,
                            "risk_replan": previous.content["risk_replan"],
                            "locale": str(checkpoint.state.get("locale", "en"))
                            if checkpoint
                            else "en",
                        },
                    )
                return await transaction.repository.request_ai_revision_mutation(
                    actor=actor,
                    proposal_id=proposal_id,
                    expected_version=expected_version,
                    job=job,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                )
        except (PlanningRunDomainError, ValueError) as error:
            async with self._transaction_factory(actor) as transaction:
                await transaction.repository.audit_rejection(
                    actor=actor,
                    action="proposal.ai_revision_requested",
                    request_id=request_id,
                    reason_code=type(error).__name__,
                    idempotency_key=idempotency_key,
                    resource_id=proposal_id,
                )
            raise
