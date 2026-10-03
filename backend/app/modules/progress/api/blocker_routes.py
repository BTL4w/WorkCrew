"""Manual blocker mutations never require an AI proposal."""

from collections.abc import Callable, Coroutine
from functools import partial
from inspect import isawaitable
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import Response
from fastapi.routing import APIRoute

from app.api.errors import ApplicationError, ErrorResponse
from app.core.config import Settings
from app.modules.identity.api.dependencies import ActorDependency, get_authenticated_actor
from app.modules.identity.application.auth_service import AuthService
from app.modules.progress.api.blocker_schemas import BlockerMutationRequest
from app.modules.progress.application.blocker_service import BlockerService
from app.modules.progress.domain.blockers import Blocker, BlockerError, BlockerTransition


def get_blocker_service(request: Request) -> BlockerService:
    return cast(BlockerService, request.app.state.blocker_service)


Service = Annotated[BlockerService, Depends(get_blocker_service)]
Key = Annotated[str | None, Header(alias="Idempotency-Key")]


class BlockerRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def preflight(request: Request) -> Response:
            if request.method in {"POST", "PATCH", "DELETE"}:
                override = request.app.dependency_overrides.get(get_authenticated_actor)
                if override is not None:
                    candidate = override()
                    actor = await candidate if isawaitable(candidate) else candidate
                else:
                    actor = await get_authenticated_actor(
                        request,
                        cast(AuthService, request.app.state.auth_service),
                        cast(Settings, request.app.state.settings),
                    )
                service = get_blocker_service(request)
                key = request.headers.get("Idempotency-Key")
                request.state.mutation_rejection_audit = partial(
                    service.audit_transport_rejection,
                    actor=actor,
                    request_id=str(request.state.request_id),
                    key=key,
                )
            return await handler(request)

        return preflight


router = APIRouter(tags=["blockers"], route_class=BlockerRoute)
_ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (400, 401, 403, 404, 409, 422)
}


def error(exc: BlockerError) -> ApplicationError:
    return ApplicationError(
        status_code=exc.status, code=exc.code, message_key=f"blocker.error.{exc.code}"
    )


@router.post("/blockers", response_model=Blocker, responses=_ERRORS)
async def apply(
    body: BlockerMutationRequest,
    actor: ActorDependency,
    service: Service,
    request: Request,
    idempotency_key: Key = None,
) -> Blocker:
    try:
        return await service.apply(
            actor, body, idempotency_key or "", str(request.state.request_id)
        )
    except BlockerError as exc:
        raise error(exc) from exc


@router.patch("/blockers/{blocker_id}", response_model=Blocker, responses=_ERRORS)
@router.delete("/blockers/{blocker_id}", response_model=Blocker, responses=_ERRORS)
async def mutate(
    blocker_id: UUID,
    body: BlockerMutationRequest,
    actor: ActorDependency,
    service: Service,
    request: Request,
    idempotency_key: Key = None,
) -> Blocker:
    if (
        body.blocker_id != blocker_id
        or body.action == "CREATE"
        or (request.method == "DELETE" and body.action != "ARCHIVE")
    ):
        await service.reject(
            actor,
            str(request.state.request_id),
            idempotency_key,
            "INVALID_BLOCKER_COMMAND",
            blocker_id,
        )
        raise error(BlockerError("INVALID_BLOCKER_COMMAND", 422))
    return await apply(body, actor, service, request, idempotency_key)


@router.get("/blockers", response_model=tuple[Blocker, ...], responses=_ERRORS)
async def list_blockers(
    task_id: UUID, actor: ActorDependency, service: Service
) -> tuple[Blocker, ...]:
    try:
        return await service.list(actor, task_id)
    except BlockerError as exc:
        raise error(exc) from exc


@router.get("/blockers/{blocker_id}", response_model=Blocker, responses=_ERRORS)
async def get_blocker(blocker_id: UUID, actor: ActorDependency, service: Service) -> Blocker:
    history = await history_blocker(blocker_id, actor, service)
    return history[-1].snapshot


@router.get(
    "/blockers/{blocker_id}/history",
    response_model=tuple[BlockerTransition, ...],
    responses=_ERRORS,
)
async def history_blocker(
    blocker_id: UUID, actor: ActorDependency, service: Service
) -> tuple[BlockerTransition, ...]:
    try:
        return await service.history(actor, blocker_id)
    except BlockerError as exc:
        raise error(exc) from exc
