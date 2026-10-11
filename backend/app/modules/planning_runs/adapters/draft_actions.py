"""Exact draft preparation inside the application's existing transaction boundary."""

from datetime import UTC, date, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.audit.domain.events import AuditOutcome
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.planning_runs.adapters.database_models import (
    ApprovalModel,
    PlanningDraftModel,
    ProposalModel,
    WorkflowRunModel,
)
from app.modules.planning_runs.adapters.draft_repository import (
    PostgreSQLPlanningDraftRepository,
    lock_planning_workflow,
)
from app.modules.planning_runs.application.ports import PlanningRuntimePort, PlanningRunTransaction
from app.modules.planning_runs.application.run_service import fingerprint
from app.modules.planning_runs.domain.drafts import PreparedPlanningAction
from app.modules.planning_runs.domain.models import (
    IdempotencyKeyReusedError,
    OutboxEvent,
    PlanningRunNotFoundError,
    Proposal,
    ProposalStatus,
    ProposalValidationError,
    ProposalVersion,
    ResourceVersionMismatchError,
    WorkflowCheckpoint,
    WorkflowEvent,
    WorkflowRunStateError,
)
from app.modules.work.adapters.database_models import IdempotencyRecordModel, IdempotencyState
from work_management_ai.schemas.progressive_planning import PlanningDraft
from work_management_ai.workflows.planning.draft_schedule import materialize_draft


async def prepare_draft_action(
    *,
    transaction: PlanningRunTransaction,
    runtime: PlanningRuntimePort,
    timezone: str,
    actor: AuthenticatedActor,
    draft_id: UUID,
    expected_draft_version: int,
    anchor_date: date,
    request_id: str,
    idempotency_key: str,
) -> PreparedPlanningAction:
    session = cast(AsyncSession, transaction.session)
    repository = transaction.repository
    drafts = PostgreSQLPlanningDraftRepository(transaction)
    draft = await session.scalar(
        select(PlanningDraftModel).where(
            PlanningDraftModel.organization_id == actor.organization_id,
            PlanningDraftModel.id == draft_id,
            PlanningDraftModel.created_by_membership_id == actor.membership_id,
        )
    )
    if draft is None:
        raise PlanningRunNotFoundError
    await drafts.authorize(actor, draft.workflow_run_id)
    await lock_planning_workflow(session, actor.organization_id, draft.workflow_run_id)
    await session.refresh(draft)
    operation = f"planning.draft.prepare:{draft_id}"
    digest = fingerprint(
        operation,
        {
            "version": expected_draft_version,
            "anchor_date": anchor_date.isoformat(),
            "timezone": timezone,
        },
    )
    replay = await session.scalar(
        select(IdempotencyRecordModel).where(
            IdempotencyRecordModel.organization_id == actor.organization_id,
            IdempotencyRecordModel.actor_membership_id == actor.membership_id,
            IdempotencyRecordModel.operation == operation,
            IdempotencyRecordModel.idempotency_key == idempotency_key,
        )
    )
    if replay is not None:
        if replay.request_fingerprint != digest or replay.response_body is None:
            raise IdempotencyKeyReusedError
        raw = replay.response_body
        return PreparedPlanningAction(
            UUID(raw["workflow_run_id"]),
            UUID(raw["proposal_id"]),
            int(raw["proposal_version"]),
            UUID(raw["approval_id"]),
            draft_id,
            int(raw["draft_version"]),
            raw["fingerprint"],
        )
    if draft.current_version_number != expected_draft_version:
        raise ResourceVersionMismatchError(draft.current_version_number)
    if anchor_date < datetime.now(ZoneInfo(timezone)).date():
        raise ValueError("a new plan requires a current or future calendar anchor")
    snapshot = await drafts.get_snapshot(actor, draft_id, expected_draft_version)
    run = await session.scalar(
        select(WorkflowRunModel)
        .where(
            WorkflowRunModel.organization_id == actor.organization_id,
            WorkflowRunModel.id == draft.workflow_run_id,
        )
        .with_for_update()
    )
    if (
        run is None
        or run.workflow_version != "3.0.0"
        or run.status not in {"DRAFT_AVAILABLE", "WAITING_FOR_DECISION"}
    ):
        raise WorkflowRunStateError("draft is not available for preparation")
    plan = materialize_draft(
        PlanningDraft.model_validate(snapshot.content.content), anchor_date, timezone
    )
    content = runtime.validate_proposal_content(plan.model_dump(mode="json"))
    validation = runtime.validate_proposal_deterministically(
        content, active_membership_ids=frozenset()
    )
    if not validation.get("can_approve"):
        raise ProposalValidationError
    existing = await session.scalar(
        select(ProposalModel)
        .where(
            ProposalModel.organization_id == actor.organization_id,
            ProposalModel.workflow_run_id == run.id,
        )
        .with_for_update()
    )
    if existing is None:
        proposal = Proposal.create(organization_id=actor.organization_id, workflow_run_id=run.id)
        number = 1
        proposal_id = proposal.id
    else:
        if existing.status not in {"STALE", "READY_FOR_DECISION", "DRAFT"}:
            raise WorkflowRunStateError("proposal cannot be prepared")
        if existing.approval_id:
            approval = await session.scalar(
                select(ApprovalModel)
                .where(
                    ApprovalModel.organization_id == actor.organization_id,
                    ApprovalModel.id == existing.approval_id,
                )
                .with_for_update()
            )
            if approval and approval.status == "PENDING":
                approval.status = "SUPERSEDED"
                approval.version += 1
                approval.updated_at = datetime.now(UTC)
            existing.superseded_approval_id = existing.approval_id
        number = existing.current_version_number + 1
        existing.status = ProposalStatus.DRAFT.value
        existing.current_version_number = number
        existing.approval_id = None
        existing.version += 1
        existing.updated_at = datetime.now(UTC)
        proposal = None
        proposal_id = existing.id
    version = ProposalVersion(
        id=uuid4(),
        organization_id=actor.organization_id,
        proposal_id=proposal_id,
        version_number=number,
        created_by_membership_id=actor.membership_id,
        content=content,
        assumptions=[item.model_dump(mode="json") for item in plan.assumptions],
        change_summary=f"Prepared from draft v{expected_draft_version}",
        field_provenance={"default": "AI_PROPOSED", "calendar_anchor": "MANAGER_SELECTED"},
        validation_result=validation,
        source_reference_snapshot=[
            {
                "resource_type": "PLANNING_DRAFT",
                "resource_id": str(draft_id),
                "version": expected_draft_version,
                "fingerprint": snapshot.fingerprint,
            }
        ],
        workflow_version="3.0.0",
        prompt_version=snapshot.content.versions.get("prompt", "progressive-planning.v1"),
        schema_version="planning-proposal.v2",
        model_reference=snapshot.content.versions.get("model", "unknown"),
        verifier_version="1.0.0",
        creator_type="AI_SYSTEM",
        source_draft_id=draft_id,
        source_draft_version=expected_draft_version,
    )
    if proposal:
        await repository.create_proposal(proposal=proposal, initial_version=version)
    else:
        await repository.append_proposal_version(version=version)
    await session.flush()
    ready = await repository.complete_proposal_revalidation(
        actor=actor,
        proposal_id=version.proposal_id,
        version_number=number,
        validation_result=validation,
        request_id=request_id,
    )
    if ready.approval_id is None:
        raise ProposalValidationError
    run.status = "WAITING_FOR_DECISION"
    run.version += 1
    run.updated_at = datetime.now(UTC)
    latest = await repository.get_latest_checkpoint(actor=actor, run_id=run.id)
    await repository.save_checkpoint(
        checkpoint=WorkflowCheckpoint(
            id=uuid4(),
            organization_id=actor.organization_id,
            workflow_run_id=run.id,
            node="await_manager_decision",
            sequence=latest.sequence + 1 if latest else 1,
            state={
                "stage": "AWAIT_MANAGER_DECISION",
                "workflow_version": "3.0.0",
                "proposal_id": str(ready.id),
                "proposal_version": number,
                "approval_id": str(ready.approval_id),
                "binding": {
                    "draft_id": str(draft_id),
                    "draft_version": expected_draft_version,
                    "fingerprint": snapshot.fingerprint,
                },
                "locale": snapshot.content.versions.get("locale", "en"),
            },
        )
    )
    event = await repository.append_event(
        event=WorkflowEvent(
            id=uuid4(),
            organization_id=actor.organization_id,
            workflow_run_id=run.id,
            sequence=0,
            event_type="proposal.ready",
            public_payload={
                "proposal_id": str(ready.id),
                "version": number,
                "approval_id": str(ready.approval_id),
                "can_approve": True,
            },
        )
    )
    await repository.enqueue_outbox_event(
        organization_id=actor.organization_id,
        event=OutboxEvent(
            id=uuid4(),
            organization_id=actor.organization_id,
            aggregate_type="workflow_run",
            aggregate_id=run.id,
            event_type="proposal.ready",
            event_id=event.id,
            payload={
                "event_id": str(event.id),
                "workflow_run_id": str(run.id),
                "envelope_version": 1,
            },
        ),
    )
    result = PreparedPlanningAction(
        run.id,
        ready.id,
        number,
        ready.approval_id,
        draft_id,
        expected_draft_version,
        snapshot.fingerprint,
    )
    session.add(
        AuditEventModel(
            id=uuid4(),
            organization_id=actor.organization_id,
            actor_membership_id=actor.membership_id,
            action="planning.draft.prepared",
            outcome=AuditOutcome.SUCCEEDED,
            resource_type="planning_draft",
            resource_id=draft_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
            before_data={},
            after_data={
                "proposal_id": str(ready.id),
                "proposal_version": number,
                "draft_version": expected_draft_version,
            },
            reason_data={},
        )
    )
    session.add(
        IdempotencyRecordModel(
            id=uuid4(),
            organization_id=actor.organization_id,
            actor_membership_id=actor.membership_id,
            operation=operation,
            idempotency_key=idempotency_key,
            request_fingerprint=digest,
            state=IdempotencyState.COMPLETED,
            response_status=200,
            response_body={
                "workflow_run_id": str(run.id),
                "proposal_id": str(ready.id),
                "proposal_version": number,
                "approval_id": str(ready.approval_id),
                "draft_version": expected_draft_version,
                "fingerprint": snapshot.fingerprint,
            },
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
    )
    await session.flush()
    return result
