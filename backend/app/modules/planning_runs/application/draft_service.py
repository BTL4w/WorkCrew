"""Versioned draft operations with current authorization and rejection audit."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict
from datetime import date
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.planning_runs.application.draft_ports import PlanningDraftRepository
from app.modules.planning_runs.application.run_service import fingerprint
from app.modules.planning_runs.domain.drafts import (
    DraftContent,
    PlanningDraftSnapshot,
    PreparedPlanningAction,
)
from app.modules.planning_runs.domain.models import (
    PlanningRunDomainError,
    PlanningRunForbiddenError,
)

DraftTransactionFactory = Callable[
    [AuthenticatedActor], AbstractAsyncContextManager[PlanningDraftRepository]
]


class PlanningDraftService:
    def __init__(self, *, transaction_factory: DraftTransactionFactory) -> None:
        self._transaction_factory = transaction_factory

    async def prepare_action(
        self,
        *,
        actor: AuthenticatedActor,
        draft_id: UUID,
        expected_draft_version: int,
        anchor_date: date,
        request_id: str,
        idempotency_key: str,
    ) -> PreparedPlanningAction:
        try:
            if actor.role not in {MembershipRole.ADMIN, MembershipRole.MANAGER}:
                raise PlanningRunForbiddenError
            if not idempotency_key or expected_draft_version < 1:
                raise ValueError("valid draft version and idempotency key are required")
            async with self._transaction_factory(actor) as repository:
                return await repository.prepare_action(
                    actor=actor,
                    draft_id=draft_id,
                    expected_draft_version=expected_draft_version,
                    anchor_date=anchor_date,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                )
        except (PlanningRunDomainError, ValueError) as error:
            async with self._transaction_factory(actor) as repository:
                await repository.audit_rejection(actor, request_id, type(error).__name__)
            raise

    async def get_snapshot(
        self, *, actor: AuthenticatedActor, draft_id: UUID, version: int | None = None
    ) -> PlanningDraftSnapshot:
        if actor.role not in {MembershipRole.ADMIN, MembershipRole.MANAGER}:
            raise PlanningRunForbiddenError
        async with self._transaction_factory(actor) as repository:
            return await repository.get_snapshot(actor, draft_id, version)

    async def publish_version(
        self,
        *,
        actor: AuthenticatedActor,
        run_id: UUID,
        expected_draft_version: int | None,
        draft: DraftContent,
        request_id: str,
        idempotency_key: str,
    ) -> PlanningDraftSnapshot:
        try:
            if actor.role not in {MembershipRole.ADMIN, MembershipRole.MANAGER}:
                raise PlanningRunForbiddenError
            if not idempotency_key or len(idempotency_key) > 128:
                raise ValueError("invalid idempotency key")
            if not draft.content:
                raise ValueError("draft content required")
            digest = fingerprint(
                "planning.draft.publish",
                {
                    "run_id": run_id,
                    "expected_version": expected_draft_version,
                    "draft": asdict(draft),
                },
            )
            async with self._transaction_factory(actor) as repository:
                return await repository.publish(
                    actor,
                    run_id,
                    expected_draft_version,
                    draft,
                    request_id,
                    idempotency_key,
                    digest,
                )
        except (PlanningRunDomainError, ValueError) as error:
            async with self._transaction_factory(actor) as repository:
                await repository.audit_rejection(actor, request_id, type(error).__name__)
            raise
