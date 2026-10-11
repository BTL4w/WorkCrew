"""Task 7 planning-run, message and immutable proposal endpoints."""

from __future__ import annotations

import re
from typing import Annotated, Any, NoReturn
from uuid import UUID

from fastapi import APIRouter, Header, Path, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from app.api.errors import ApplicationError, ErrorResponse
from app.modules.identity.api.dependencies import ActorDependency
from app.modules.planning_runs.api.dependencies import (
    ApprovalServiceDependency,
    PlanningDraftServiceDependency,
    PlanningRunServiceDependency,
    ProposalServiceDependency,
    WorkflowEventServiceDependency,
)
from app.modules.planning_runs.api.schemas import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ManagerMessageRequest,
    PlanningDraftResponse,
    PlanningRunCreateRequest,
    ProposalEditRequest,
    ProposalReferenceResponse,
    ProposalVersionResponse,
    WorkflowRunListResponse,
    WorkflowRunReferenceResponse,
    WorkflowRunResponse,
)
from app.modules.planning_runs.domain.models import (
    ApprovalStateConflictError,
    IdempotencyKeyReusedError,
    PlanningRunDomainError,
    PlanningRunForbiddenError,
    PlanningRunNotFoundError,
    ProposalStaleError,
    ProposalValidationError,
    ResourceVersionMismatchError,
    UnsupportedPlanningCapabilityError,
    WorkflowRunStateError,
)

router = APIRouter(tags=["AI planning"])

IdempotencyKeyHeader = Annotated[
    str, Header(alias="Idempotency-Key", min_length=16, max_length=128)
]
IfMatchHeader = Annotated[str | None, Header(alias="If-Match")]
_VERSION_ETAG = re.compile(r'^"([1-9][0-9]*)"$')
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (400, 401, 403, 404, 409, 412, 422, 428)
}


def _expected_version(value: str | None) -> int:
    if value is None:
        raise ApplicationError(
            status_code=428,
            code="PRECONDITION_REQUIRED",
            message_key="common.error.preconditionRequired",
        )
    match = _VERSION_ETAG.fullmatch(value)
    if match is None:
        raise ApplicationError(
            status_code=400,
            code="INVALID_REQUEST",
            message_key="common.error.invalidRequest",
        )
    return int(match.group(1))


def _raise_planning_error(error: Exception) -> NoReturn:
    if isinstance(error, PlanningRunForbiddenError):
        mapped = ApplicationError(
            status_code=403, code="FORBIDDEN", message_key="common.error.forbidden"
        )
    elif isinstance(error, PlanningRunNotFoundError):
        mapped = ApplicationError(
            status_code=404,
            code="RESOURCE_NOT_FOUND",
            message_key="common.error.notFound",
        )
    elif isinstance(error, ResourceVersionMismatchError):
        mapped = ApplicationError(
            status_code=412,
            code="RESOURCE_VERSION_MISMATCH",
            message_key="common.error.resourceVersionMismatch",
            details={"current_version": error.current_version},
        )
    elif isinstance(error, IdempotencyKeyReusedError):
        mapped = ApplicationError(
            status_code=409,
            code="IDEMPOTENCY_KEY_REUSED",
            message_key="common.error.idempotencyKeyReused",
        )
    elif isinstance(error, WorkflowRunStateError):
        mapped = ApplicationError(
            status_code=409,
            code="WORKFLOW_STATE_CONFLICT",
            message_key="ai.error.workflowStateConflict",
        )
    elif isinstance(error, ApprovalStateConflictError):
        mapped = ApplicationError(
            status_code=409,
            code="APPROVAL_STATE_CONFLICT",
            message_key="approval.error.stateConflict",
        )
    elif isinstance(error, ProposalStaleError):
        mapped = ApplicationError(
            status_code=422,
            code="PROPOSAL_STALE",
            message_key="approval.error.proposalStale",
        )
    elif isinstance(error, ProposalValidationError):
        mapped = ApplicationError(
            status_code=422,
            code="VALIDATION_FAILED",
            message_key="common.error.validation",
        )
    elif isinstance(error, UnsupportedPlanningCapabilityError):
        mapped = ApplicationError(
            status_code=422,
            code="UNSUPPORTED_CAPABILITY",
            message_key="ai.error.unsupportedCapability",
        )
    elif isinstance(error, ValueError):
        mapped = ApplicationError(
            status_code=422,
            code="VALIDATION_FAILED",
            message_key="common.error.validation",
        )
    else:
        raise error
    raise mapped from error


@router.post(
    "/ai/planning-runs",
    response_model=WorkflowRunReferenceResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def create_planning_run(
    payload: PlanningRunCreateRequest,
    request: Request,
    response: Response,
    actor: ActorDependency,
    service: PlanningRunServiceDependency,
    idempotency_key: IdempotencyKeyHeader,
) -> WorkflowRunReferenceResponse:
    try:
        result = await service.create_planning_run(
            actor=actor,
            message=payload.message,
            locale=payload.locale,
            request_id=str(request.state.request_id),
            idempotency_key=idempotency_key,
        )
    except (PlanningRunDomainError, ValueError) as error:
        _raise_planning_error(error)
    response.headers["Location"] = f"/api/v1/workflow-runs/{result.run.id}"
    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return WorkflowRunReferenceResponse.from_domain(result.run)


@router.get(
    "/workflow-runs",
    response_model=WorkflowRunListResponse,
    responses=_ERROR_RESPONSES,
)
async def list_workflow_runs(
    actor: ActorDependency,
    service: PlanningRunServiceDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> WorkflowRunListResponse:
    try:
        runs = await service.list_workflow_runs(actor=actor, limit=limit)
    except PlanningRunDomainError as error:
        _raise_planning_error(error)
    return WorkflowRunListResponse(items=[WorkflowRunResponse.from_domain(run) for run in runs])


@router.get(
    "/workflow-runs/{run_id}",
    response_model=WorkflowRunResponse,
    responses=_ERROR_RESPONSES,
)
async def get_workflow_run(
    run_id: UUID,
    actor: ActorDependency,
    service: PlanningRunServiceDependency,
    response: Response,
) -> WorkflowRunResponse:
    try:
        snapshot = await service.get_workflow_run_snapshot(actor=actor, run_id=run_id)
    except PlanningRunDomainError as error:
        _raise_planning_error(error)
    response.headers["ETag"] = f'"{snapshot.run.version}"'
    return WorkflowRunResponse.from_snapshot(snapshot)


@router.post(
    "/workflow-runs/{run_id}/messages",
    response_model=WorkflowRunReferenceResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def post_manager_message(
    run_id: UUID,
    payload: ManagerMessageRequest,
    request: Request,
    response: Response,
    actor: ActorDependency,
    service: PlanningRunServiceDependency,
    idempotency_key: IdempotencyKeyHeader,
) -> WorkflowRunReferenceResponse:
    try:
        result = await service.post_manager_message(
            actor=actor,
            run_id=run_id,
            message=payload.message,
            request_id=str(request.state.request_id),
            idempotency_key=idempotency_key,
        )
    except (PlanningRunDomainError, ValueError) as error:
        _raise_planning_error(error)
    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return WorkflowRunReferenceResponse.from_domain(result.run)


@router.get(
    "/proposals/{proposal_id}/versions/{version_number}",
    response_model=ProposalVersionResponse,
    responses=_ERROR_RESPONSES,
)
async def get_proposal_version(
    proposal_id: UUID,
    version_number: Annotated[int, Path(ge=1)],
    actor: ActorDependency,
    service: ProposalServiceDependency,
) -> ProposalVersionResponse:
    try:
        proposal, version = await service.get_proposal_version(
            actor=actor,
            proposal_id=proposal_id,
            version_number=version_number,
        )
    except PlanningRunDomainError as error:
        _raise_planning_error(error)
    return ProposalVersionResponse.from_domain(proposal, version)


@router.patch(
    "/proposals/{proposal_id}",
    response_model=ProposalReferenceResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def edit_proposal(
    proposal_id: UUID,
    payload: ProposalEditRequest,
    request: Request,
    response: Response,
    actor: ActorDependency,
    service: ProposalServiceDependency,
    idempotency_key: IdempotencyKeyHeader,
    if_match: IfMatchHeader = None,
) -> ProposalReferenceResponse:
    try:
        result = await service.edit_proposal(
            actor=actor,
            proposal_id=proposal_id,
            expected_version=_expected_version(if_match),
            content=payload.content,
            request_id=str(request.state.request_id),
            idempotency_key=idempotency_key,
        )
    except (PlanningRunDomainError, ValueError) as error:
        _raise_planning_error(error)
    response.headers["ETag"] = f'"{result.version.version_number}"'
    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return ProposalReferenceResponse.from_domain(result.proposal, result.version)


@router.post(
    "/approvals/{approval_id}/decision",
    response_model=ApprovalDecisionResponse,
    responses=_ERROR_RESPONSES,
)
async def decide_approval(
    approval_id: UUID,
    payload: ApprovalDecisionRequest,
    request: Request,
    response: Response,
    actor: ActorDependency,
    service: ApprovalServiceDependency,
    idempotency_key: IdempotencyKeyHeader,
    if_match: IfMatchHeader = None,
) -> ApprovalDecisionResponse:
    try:
        result = await service.decide(
            actor=actor,
            approval_id=approval_id,
            decision=payload.decision,
            expected_proposal_version=_expected_version(if_match),
            reason=payload.reason,
            request_id=str(request.state.request_id),
            idempotency_key=idempotency_key,
        )
    except (PlanningRunDomainError, ValueError) as error:
        _raise_planning_error(error)
    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return ApprovalDecisionResponse.from_result(result)


@router.get(
    "/workflow-runs/{run_id}/events",
    responses={
        **_ERROR_RESPONSES,
        200: {"content": {"text/event-stream": {}}},
    },
)
async def stream_workflow_events(
    run_id: UUID,
    actor: ActorDependency,
    service: WorkflowEventServiceDependency,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    after_sequence = 0
    if last_event_id is not None:
        try:
            after_sequence = int(last_event_id)
        except ValueError as error:
            raise ApplicationError(
                status_code=400,
                code="INVALID_LAST_EVENT_ID",
                message_key="ai.error.invalidLastEventId",
            ) from error
        if after_sequence < 0:
            raise ApplicationError(
                status_code=400,
                code="INVALID_LAST_EVENT_ID",
                message_key="ai.error.invalidLastEventId",
            )
    try:
        await service.authorize(actor=actor, run_id=run_id)
    except PlanningRunDomainError as error:
        _raise_planning_error(error)
    return StreamingResponse(
        service.stream(
            actor=actor,
            run_id=run_id,
            after_sequence=after_sequence,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _draft_snapshot(
    actor: ActorDependency,
    service: PlanningDraftServiceDependency,
    draft_id: UUID,
    version: int | None = None,
) -> PlanningDraftResponse:
    try:
        snapshot = await service.get_snapshot(actor=actor, draft_id=draft_id, version=version)
    except PlanningRunDomainError as error:
        _raise_planning_error(error)
    return PlanningDraftResponse(
        draft_id=snapshot.draft_id,
        workflow_run_id=snapshot.workflow_run_id,
        draft_version=snapshot.draft_version,
        current_version=snapshot.current_version,
        can_prepare=snapshot.can_prepare,
        suggested_anchor_date=snapshot.suggested_anchor_date,
        fingerprint=snapshot.fingerprint,
        content=snapshot.content.content,
        question=snapshot.content.question,
        validation=snapshot.content.validation,
        versions=snapshot.content.versions,
        change_summary=snapshot.content.change_summary,
    )


@router.get("/planning-drafts/{draft_id}", response_model=PlanningDraftResponse)
async def get_planning_draft(
    actor: ActorDependency, service: PlanningDraftServiceDependency, draft_id: UUID
) -> PlanningDraftResponse:
    return await _draft_snapshot(actor, service, draft_id)


@router.get("/planning-drafts/{draft_id}/versions/{version}", response_model=PlanningDraftResponse)
async def get_planning_draft_version(
    actor: ActorDependency,
    service: PlanningDraftServiceDependency,
    draft_id: UUID,
    version: Annotated[int, Path(ge=1)],
) -> PlanningDraftResponse:
    return await _draft_snapshot(actor, service, draft_id, version)
