"""Authorized, transactional planning-run commands and snapshots."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Literal
from uuid import UUID, uuid4

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.planning_runs.application.ports import (
    PlanningRuntimePort,
    PlanningRunTransaction,
    WorkflowRunMutationResult,
    WorkflowRunSnapshot,
)
from app.modules.planning_runs.domain.models import (
    PlanningRunDomainError,
    PlanningRunForbiddenError,
    PlanningRunNotFoundError,
    WorkflowJob,
    WorkflowJobStatus,
    WorkflowRun,
    WorkflowRunStateError,
    WorkflowRunStatus,
)

_WRITE_ROLES = frozenset({MembershipRole.ADMIN, MembershipRole.MANAGER})


def fingerprint(operation: str, values: dict[str, object]) -> str:
    canonical = json.dumps(
        {"operation": operation, "values": values},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class PlanningRunService:
    def __init__(
        self,
        *,
        transaction_factory: Callable[[AuthenticatedActor], PlanningRunTransaction],
        runtime: PlanningRuntimePort,
    ) -> None:
        self._transaction_factory = transaction_factory
        self._runtime = runtime

    async def _require_writer(
        self, *, actor: AuthenticatedActor, action: str, request_id: str
    ) -> None:
        if actor.role in _WRITE_ROLES:
            return
        async with self._transaction_factory(actor) as transaction:
            await transaction.repository.audit_rejection(
                actor=actor,
                action=action,
                request_id=request_id,
                reason_code="FORBIDDEN",
            )
        raise PlanningRunForbiddenError

    async def _audit_mutation_error(
        self,
        *,
        actor: AuthenticatedActor,
        action: str,
        request_id: str,
        idempotency_key: str,
        resource_id: UUID | None,
        error: Exception,
    ) -> None:
        async with self._transaction_factory(actor) as transaction:
            await transaction.repository.audit_rejection(
                actor=actor,
                action=action,
                request_id=request_id,
                reason_code=type(error).__name__,
                idempotency_key=idempotency_key,
                resource_id=resource_id,
            )

    async def create_planning_run(
        self,
        *,
        actor: AuthenticatedActor,
        message: str,
        locale: Literal["vi", "en"],
        request_id: str,
        idempotency_key: str,
    ) -> WorkflowRunMutationResult:
        await self._require_writer(
            actor=actor, action="planning_run.created", request_id=request_id
        )
        try:
            normalized = message.strip()
            if not normalized or len(normalized) > 8000:
                raise ValueError("planning message is invalid")
            self._runtime.validate_capability(normalized)
            run = WorkflowRun.create(
                organization_id=actor.organization_id,
                project_id=None,
                requested_by_membership_id=actor.membership_id,
                workflow_name="project_planning",
                workflow_version=self._runtime.workflow_version,
                verifier_version=self._runtime.verifier_version,
                input_goal_text=normalized,
            )
            job = WorkflowJob(
                id=uuid4(),
                organization_id=actor.organization_id,
                workflow_run_id=run.id,
                job_type="planning.start",
                status=WorkflowJobStatus.QUEUED,
                payload={
                    "instruction": "START",
                    "locale": locale,
                    "actor_role": actor.role.value,
                },
            )
            request_fingerprint = fingerprint(
                "planning_run.create", {"message": normalized, "locale": locale}
            )
            async with self._transaction_factory(actor) as transaction:
                return await transaction.repository.create_planning_run_mutation(
                    actor=actor,
                    run=run,
                    job=job,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                )
        except (PlanningRunDomainError, ValueError) as error:
            await self._audit_mutation_error(
                actor=actor,
                action="planning_run.created",
                request_id=request_id,
                idempotency_key=idempotency_key,
                resource_id=None,
                error=error,
            )
            raise

    async def list_workflow_runs(
        self, *, actor: AuthenticatedActor, limit: int = 20
    ) -> tuple[WorkflowRun, ...]:
        await self._require_writer(actor=actor, action="planning_run.listed", request_id="read")
        async with self._transaction_factory(actor) as transaction:
            return await transaction.repository.list_workflow_runs(
                actor=actor, limit=min(max(limit, 1), 100)
            )

    async def get_workflow_run(self, *, actor: AuthenticatedActor, run_id: UUID) -> WorkflowRun:
        await self._require_writer(actor=actor, action="planning_run.viewed", request_id="read")
        async with self._transaction_factory(actor) as transaction:
            run = await transaction.repository.get_workflow_run(actor=actor, run_id=run_id)
        if run is None:
            raise PlanningRunNotFoundError
        return run

    async def get_workflow_run_snapshot(
        self, *, actor: AuthenticatedActor, run_id: UUID
    ) -> WorkflowRunSnapshot:
        await self._require_writer(actor=actor, action="planning_run.viewed", request_id="read")
        async with self._transaction_factory(actor) as transaction:
            repository = transaction.repository
            run = await repository.get_workflow_run(actor=actor, run_id=run_id)
            if run is None:
                raise PlanningRunNotFoundError
            checkpoint = (
                await repository.get_latest_checkpoint(
                    actor=actor, run_id=run_id, include_state=False
                )
                if run.error_message == "CONTEXT_EXPIRED"
                else await repository.get_latest_checkpoint(actor=actor, run_id=run_id)
            )
            proposal = await repository.get_proposal_by_run_id(actor=actor, run_id=run_id)
            proposal_version = None
            previous_proposal_version = None
            if proposal is not None:
                proposal_version = await repository.get_proposal_version(
                    actor=actor,
                    proposal_id=proposal.id,
                    version_number=proposal.current_version_number,
                )
                if proposal.current_version_number > 1:
                    previous_proposal_version = await repository.get_proposal_version(
                        actor=actor,
                        proposal_id=proposal.id,
                        version_number=proposal.current_version_number - 1,
                    )
            events = await repository.list_events(actor=actor, run_id=run_id)
        return WorkflowRunSnapshot(
            run=run,
            checkpoint=checkpoint,
            proposal=proposal,
            proposal_version=proposal_version,
            events=tuple(events),
            previous_proposal_version=previous_proposal_version,
        )

    async def post_manager_message(
        self,
        *,
        actor: AuthenticatedActor,
        run_id: UUID,
        message: str,
        request_id: str,
        idempotency_key: str,
    ) -> WorkflowRunMutationResult:
        await self._require_writer(
            actor=actor,
            action="planning_run.message_submitted",
            request_id=request_id,
        )
        try:
            normalized = message.strip()
            if not normalized or len(normalized) > 8000:
                raise ValueError("manager message is invalid")
            request_fingerprint = fingerprint(
                "planning_run.message",
                {"run_id": str(run_id), "message": normalized},
            )
            async with self._transaction_factory(actor) as transaction:
                repository = transaction.repository
                replay = await repository.find_workflow_run_mutation_replay(
                    actor=actor,
                    operation=f"planning_run.message:{run_id}",
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                )
                if replay is not None:
                    return replay
                run = await repository.get_workflow_run(actor=actor, run_id=run_id)
                if run is None:
                    raise PlanningRunNotFoundError
                if run.status is not WorkflowRunStatus.NEEDS_INPUT:
                    raise WorkflowRunStateError("run is not awaiting manager input")
                checkpoint = await repository.get_latest_checkpoint(actor=actor, run_id=run_id)
                if checkpoint is None or checkpoint.node != "await_manager_input":
                    raise WorkflowRunStateError("checkpoint is not await_manager_input")
                updated_run = run.mark_running()
                job = WorkflowJob(
                    id=uuid4(),
                    organization_id=actor.organization_id,
                    workflow_run_id=run.id,
                    job_type="planning.resume",
                    status=WorkflowJobStatus.QUEUED,
                    payload={
                        "instruction": "RESUME_MANAGER_INPUT",
                        "checkpoint_sequence": checkpoint.sequence,
                        "manager_message": normalized,
                    },
                )
                return await repository.resume_planning_run_mutation(
                    actor=actor,
                    run=updated_run,
                    job=job,
                    checkpoint=checkpoint,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                )
        except (PlanningRunDomainError, ValueError) as error:
            await self._audit_mutation_error(
                actor=actor,
                action="planning_run.message_submitted",
                request_id=request_id,
                idempotency_key=idempotency_key,
                resource_id=run_id,
                error=error,
            )
            raise
