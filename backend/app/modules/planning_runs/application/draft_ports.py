"""Application-owned ports for permission-scoped operational drafts."""

from datetime import date
from typing import Protocol
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.planning_runs.domain.drafts import (
    DraftContent,
    PlanningDraftSnapshot,
    PreparedPlanningAction,
)


class PlanningDraftRepository(Protocol):
    async def prepare_action(
        self,
        *,
        actor: AuthenticatedActor,
        draft_id: UUID,
        expected_draft_version: int,
        anchor_date: date,
        request_id: str,
        idempotency_key: str,
    ) -> PreparedPlanningAction: ...

    async def authorize(self, actor: AuthenticatedActor, run_id: UUID) -> None: ...
    async def get_snapshot(
        self, actor: AuthenticatedActor, draft_id: UUID, version: int | None
    ) -> PlanningDraftSnapshot: ...
    async def publish(
        self,
        actor: AuthenticatedActor,
        run_id: UUID,
        expected_version: int | None,
        draft: DraftContent,
        request_id: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> PlanningDraftSnapshot: ...
    async def audit_rejection(
        self, actor: AuthenticatedActor, request_id: str, code: str
    ) -> None: ...
