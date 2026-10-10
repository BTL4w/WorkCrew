"""Assistant conversation REST API and persisted SSE endpoints — Task 6.

Routes:
  POST   /api/v1/ai/conversations
  GET    /api/v1/ai/conversations
  GET    /api/v1/ai/conversations/{conversation_id}
  POST   /api/v1/ai/conversations/{conversation_id}/messages
  GET    /api/v1/ai/conversations/{conversation_id}/events   (SSE)

Security:
  - All endpoints require authenticated session.
  - Owner-only: non-disclosing 404 for foreign resources.
  - No Agent/Tool ID, role flag or approval flag in the public API.

Idempotency:
  - POST /conversations: requires Idempotency-Key (16..128 chars).
  - POST /conversations/{id}/messages: requires Idempotency-Key.
  - If-Match: FORBIDDEN for plain messages, optional for PLANNING_INPUT,
    REQUIRED for PLANNING_REVISE.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, NoReturn
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from app.api.errors import ApplicationError, ErrorResponse
from app.modules.assistant.api.dependencies import (
    AssistantEventServiceDependency,
    AssistantServiceDependency,
)
from app.modules.assistant.api.schemas import (
    AssistantTurnAcceptedResponse,
    ConversationListResponse,
    ConversationResponse,
    ConversationSnapshotResponse,
    CreateConversationRequest,
    MessageResponse,
    PostAssistantMessageRequest,
    UpdateConversationRequest,
)
from app.modules.assistant.application.service import (
    AssistantServiceError,
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from app.modules.identity.api.dependencies import ActorDependency

router = APIRouter(tags=["AI assistant"])

IdempotencyKeyHeader = Annotated[
    str, Header(alias="Idempotency-Key", min_length=16, max_length=128)
]
IfMatchHeader = Annotated[str | None, Header(alias="If-Match")]

_VERSION_ETAG = re.compile(r'^"([1-9][0-9]*)"$')

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (400, 401, 403, 404, 409, 412, 422, 428)
}


def _expected_version_optional(value: str | None) -> int | None:
    """Parse optional If-Match header → version integer or None."""
    if value is None:
        return None
    match = _VERSION_ETAG.fullmatch(value)
    if match is None:
        raise ApplicationError(
            status_code=400,
            code="INVALID_REQUEST",
            message_key="common.error.invalidRequest",
        )
    return int(match.group(1))


def _raise_assistant_error(error: Exception) -> NoReturn:
    if isinstance(error, ResourceNotFoundError):
        raise ApplicationError(
            status_code=404,
            code="RESOURCE_NOT_FOUND",
            message_key="common.error.notFound",
        ) from error
    if isinstance(error, IdempotencyConflictError):
        raise ApplicationError(
            status_code=409,
            code="IDEMPOTENCY_KEY_REUSED",
            message_key="common.error.idempotencyKeyReused",
        ) from error
    if isinstance(error, AssistantServiceError):
        code = error.code
        if code in {"INVALID_REQUEST", "CONVERSATION_BUSY"}:
            raise ApplicationError(
                status_code=409 if code == "CONVERSATION_BUSY" else 400,
                code=code,
                message_key="common.error.invalidRequest",
            ) from error
        if code == "IF_MATCH_FORBIDDEN":
            raise ApplicationError(
                status_code=400,
                code="IF_MATCH_FORBIDDEN",
                message_key="common.error.invalidRequest",
            ) from error
        if code == "IF_MATCH_REQUIRED":
            raise ApplicationError(
                status_code=428,
                code="PRECONDITION_REQUIRED",
                message_key="common.error.preconditionRequired",
            ) from error
        if code == "RESOURCE_VERSION_MISMATCH":
            raise ApplicationError(
                status_code=412,
                code="RESOURCE_VERSION_MISMATCH",
                message_key="common.error.resourceVersionMismatch",
            ) from error
        if code in {"PROPOSAL_ID_REQUIRED", "RECOMMENDATION_ID_REQUIRED"}:
            raise ApplicationError(
                status_code=400,
                code=code,
                message_key="common.error.invalidRequest",
            ) from error
        raise ApplicationError(
            status_code=503,
            code="ASSISTANT_UNAVAILABLE",
            message_key="ai.error.assistantUnavailable",
        ) from error
    raise error


# ---------------------------------------------------------------------------
# POST /ai/conversations
# ---------------------------------------------------------------------------


@router.post(
    "/ai/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_ERROR_RESPONSES,
)
async def create_conversation(
    payload: CreateConversationRequest,
    request: Request,
    response: Response,
    actor: ActorDependency,
    service: AssistantServiceDependency,
    idempotency_key: IdempotencyKeyHeader,
) -> ConversationResponse:
    try:
        result = await service.create_conversation(
            actor=actor,
            locale=payload.locale,
            title=payload.title,
            request_id=str(request.state.request_id),
            idempotency_key=idempotency_key,
        )
    except (AssistantServiceError, Exception) as error:
        _raise_assistant_error(error)
    response.headers["Location"] = f"/api/v1/ai/conversations/{result.conversation.id}"
    response.headers["ETag"] = f'"{result.conversation.version}"'
    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return ConversationResponse.from_domain(result.conversation)


# ---------------------------------------------------------------------------
# GET /ai/conversations
# ---------------------------------------------------------------------------


@router.get(
    "/ai/conversations",
    response_model=ConversationListResponse,
    responses=_ERROR_RESPONSES,
)
async def list_conversations(
    actor: ActorDependency,
    service: AssistantServiceDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ConversationListResponse:
    try:
        conversations = await service.list_conversations(actor=actor, limit=limit)
    except AssistantServiceError as error:
        _raise_assistant_error(error)
    return ConversationListResponse(
        items=[ConversationResponse.from_domain(c) for c in conversations]
    )


# ---------------------------------------------------------------------------
# GET /ai/conversations/{conversation_id}
# ---------------------------------------------------------------------------


@router.get(
    "/ai/conversations/{conversation_id}",
    response_model=ConversationSnapshotResponse,
    responses=_ERROR_RESPONSES,
)
async def get_conversation(
    conversation_id: UUID,
    actor: ActorDependency,
    service: AssistantServiceDependency,
    response: Response,
) -> ConversationSnapshotResponse:
    try:
        snapshot = await service.get_conversation(actor=actor, conversation_id=conversation_id)
    except AssistantServiceError as error:
        _raise_assistant_error(error)
    response.headers["ETag"] = f'"{snapshot.conversation.version}"'
    return ConversationSnapshotResponse(
        conversation=ConversationResponse.from_domain(snapshot.conversation),
        messages=[MessageResponse.from_domain(m) for m in snapshot.messages],
    )


# ---------------------------------------------------------------------------
# POST /ai/conversations/{conversation_id}/messages
# ---------------------------------------------------------------------------


@router.post(
    "/ai/conversations/{conversation_id}/messages",
    response_model=AssistantTurnAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def post_message(
    conversation_id: UUID,
    payload: PostAssistantMessageRequest,
    request: Request,
    response: Response,
    actor: ActorDependency,
    service: AssistantServiceDependency,
    idempotency_key: IdempotencyKeyHeader,
    if_match: IfMatchHeader = None,
) -> AssistantTurnAcceptedResponse:
    if_match_version = _expected_version_optional(if_match)
    card_dict = (
        payload.card_action.model_dump(mode="json", exclude_none=True)
        if payload.card_action
        else None
    )
    try:
        result = await service.post_message(
            actor=actor,
            conversation_id=conversation_id,
            message=payload.message,
            locale=payload.locale,
            card_action=card_dict,
            if_match_version=if_match_version,
            request_id=str(request.state.request_id),
            idempotency_key=idempotency_key,
        )
    except (AssistantServiceError, Exception) as error:
        _raise_assistant_error(error)
    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return AssistantTurnAcceptedResponse(
        conversation_id=conversation_id,
        message_id=result.message.id,
        turn_id=result.turn.id,
        orchestration_run_id=result.run.id,
        status="QUEUED",
    )


# ---------------------------------------------------------------------------
# GET /ai/conversations/{conversation_id}/events  (SSE)
# ---------------------------------------------------------------------------


@router.get(
    "/ai/conversations/{conversation_id}/events",
    responses={
        **_ERROR_RESPONSES,
        200: {"content": {"text/event-stream": {}}},
    },
)
async def stream_conversation_events(
    conversation_id: UUID,
    actor: ActorDependency,
    service: AssistantEventServiceDependency,
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
        await service.authorize(actor=actor, conversation_id=conversation_id)
    except ResourceNotFoundError as error:
        _raise_assistant_error(error)

    return StreamingResponse(
        service.stream(
            actor=actor,
            conversation_id=conversation_id,
            after_sequence=after_sequence,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _manage_conversation(
    conversation_id: UUID,
    changes: dict[str, object],
    request: Request,
    response: Response,
    actor: ActorDependency,
    service: AssistantServiceDependency,
    idempotency_key: str,
    if_match: str | None,
) -> ConversationResponse:
    version = _expected_version_optional(if_match)
    try:
        if version is None:
            raise AssistantServiceError("IF_MATCH_REQUIRED")
        result = await service.manage_conversation(
            actor=actor,
            conversation_id=conversation_id,
            changes=changes,
            expected_version=version,
            request_id=str(request.state.request_id),
            idempotency_key=idempotency_key,
        )
    except Exception as error:
        _raise_assistant_error(error)
    response.headers["ETag"] = f'"{result.conversation.version}"'
    if result.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return ConversationResponse.from_domain(result.conversation)


@router.patch(
    "/ai/conversations/{conversation_id}",
    response_model=ConversationResponse,
    responses=_ERROR_RESPONSES,
)
async def update_conversation(
    conversation_id: UUID,
    payload: UpdateConversationRequest,
    request: Request,
    response: Response,
    actor: ActorDependency,
    service: AssistantServiceDependency,
    idempotency_key: IdempotencyKeyHeader,
    if_match: IfMatchHeader = None,
) -> ConversationResponse:
    return await _manage_conversation(
        conversation_id,
        payload.model_dump(exclude_unset=True),
        request,
        response,
        actor,
        service,
        idempotency_key,
        if_match,
    )


@router.delete(
    "/ai/conversations/{conversation_id}",
    response_model=ConversationResponse,
    responses=_ERROR_RESPONSES,
)
async def delete_conversation(
    conversation_id: UUID,
    request: Request,
    response: Response,
    actor: ActorDependency,
    service: AssistantServiceDependency,
    idempotency_key: IdempotencyKeyHeader,
    if_match: IfMatchHeader = None,
) -> ConversationResponse:
    return await _manage_conversation(
        conversation_id,
        {"status": "ARCHIVED"},
        request,
        response,
        actor,
        service,
        idempotency_key,
        if_match,
    )
