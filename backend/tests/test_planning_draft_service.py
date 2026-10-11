"""Draft publication is versioned operational state, never a business mutation."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date
from uuid import UUID, uuid4

import pytest

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.planning_runs.application.draft_service import PlanningDraftService
from app.modules.planning_runs.domain.drafts import (
    DraftContent,
    PlanningDraftSnapshot,
    PreparedPlanningAction,
)
from app.modules.planning_runs.domain.models import (
    IdempotencyKeyReusedError,
    PlanningRunForbiddenError,
    PlanningRunNotFoundError,
    ResourceVersionMismatchError,
)


def manager() -> AuthenticatedActor:
    return AuthenticatedActor(
        uuid4(),
        "manager@example.test",
        "Manager",
        uuid4(),
        uuid4(),
        "Tenant",
        MembershipRole.MANAGER,
    )


class DraftStore:
    def __init__(self, actor: AuthenticatedActor) -> None:
        self.actor = actor
        self.run_id = uuid4()
        self.active = True
        self.versions: dict[int, PlanningDraftSnapshot] = {}
        self.replays: dict[str, tuple[str, PlanningDraftSnapshot]] = {}
        self.events: list[int] = []
        self.rejections: list[str] = []

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
        raise ValueError("mock store does not materialize actions")

    async def authorize(self, actor: AuthenticatedActor, run_id: UUID) -> None:
        if not self.active or actor.role is MembershipRole.EMPLOYEE:
            raise PlanningRunForbiddenError
        if (
            actor.organization_id != self.actor.organization_id
            or actor.membership_id != self.actor.membership_id
            or run_id != self.run_id
        ):
            raise PlanningRunNotFoundError

    async def get_snapshot(
        self, actor: AuthenticatedActor, draft_id: UUID, version: int | None
    ) -> PlanningDraftSnapshot:
        await self.authorize(actor, self.run_id)
        latest = self.versions[max(self.versions)]
        if latest.draft_id != draft_id:
            raise PlanningRunNotFoundError
        snapshot = self.versions.get(version or latest.draft_version)
        if snapshot is None:
            raise PlanningRunNotFoundError
        return replace(snapshot, current_version=latest.draft_version)

    async def publish(
        self,
        actor: AuthenticatedActor,
        run_id: UUID,
        expected_version: int | None,
        draft: DraftContent,
        request_id: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> PlanningDraftSnapshot:
        await self.authorize(actor, run_id)
        if idempotency_key in self.replays:
            old, snapshot = self.replays[idempotency_key]
            if old != fingerprint:
                raise IdempotencyKeyReusedError
            return snapshot
        current = max(self.versions, default=0)
        if expected_version != (current or None):
            raise ResourceVersionMismatchError(current)
        snapshot = PlanningDraftSnapshot(
            draft_id=self.versions[1].draft_id if current else uuid4(),
            workflow_run_id=run_id,
            draft_version=current + 1,
            current_version=current + 1,
            fingerprint=fingerprint,
            content=draft,
        )
        self.versions[current + 1] = snapshot
        self.replays[idempotency_key] = (fingerprint, snapshot)
        self.events.append(current + 1)
        return snapshot

    async def audit_rejection(self, actor: AuthenticatedActor, request_id: str, code: str) -> None:
        self.rejections.append(code)


@pytest.fixture
def setup() -> tuple[PlanningDraftService, DraftStore, AuthenticatedActor]:
    actor = manager()
    store = DraftStore(actor)

    @asynccontextmanager
    async def transaction(_: AuthenticatedActor) -> AsyncGenerator[DraftStore]:
        yield store

    return PlanningDraftService(transaction_factory=transaction), store, actor


async def publish(
    service: PlanningDraftService,
    store: DraftStore,
    actor: AuthenticatedActor,
    version: int | None = None,
    key: str = "one",
    value: str = "Course",
) -> PlanningDraftSnapshot:
    return await service.publish_version(
        actor=actor,
        run_id=store.run_id,
        expected_draft_version=version,
        draft=DraftContent(content={"objective": value}),
        request_id="request",
        idempotency_key=key,
    )


@pytest.mark.asyncio
async def test_update_and_refresh_restore_current_and_immutable_versions(
    setup: tuple[PlanningDraftService, DraftStore, AuthenticatedActor],
) -> None:
    service, store, actor = setup
    first = await publish(service, store, actor)
    second = await publish(service, store, actor, 1, "two", "Course revised")
    assert second.draft_id == first.draft_id and second.draft_version == 2
    assert (await service.get_snapshot(actor=actor, draft_id=first.draft_id)).content.content == {
        "objective": "Course revised"
    }
    old = await service.get_snapshot(actor=actor, draft_id=first.draft_id, version=1)
    assert old.content.content == {"objective": "Course"} and old.current_version == 2


@pytest.mark.asyncio
async def test_retry_after_commit_does_not_duplicate_version_or_event(
    setup: tuple[PlanningDraftService, DraftStore, AuthenticatedActor],
) -> None:
    service, store, actor = setup
    first = await publish(service, store, actor)
    assert await publish(service, store, actor) == first
    assert store.events == [1] and len(store.versions) == 1
    with pytest.raises(IdempotencyKeyReusedError):
        await publish(service, store, actor, value="Other")


@pytest.mark.asyncio
async def test_stale_update_is_rejected_and_audited(
    setup: tuple[PlanningDraftService, DraftStore, AuthenticatedActor],
) -> None:
    service, store, actor = setup
    await publish(service, store, actor)
    with pytest.raises(ResourceVersionMismatchError):
        await publish(service, store, actor, None, "stale")
    assert store.events == [1]
    assert "ResourceVersionMismatchError" in store.rejections


@pytest.mark.asyncio
async def test_foreign_owner_employee_and_revoked_membership_cannot_read(
    setup: tuple[PlanningDraftService, DraftStore, AuthenticatedActor],
) -> None:
    service, store, actor = setup
    first = await publish(service, store, actor)
    for foreign in [replace(actor, organization_id=uuid4()), replace(actor, membership_id=uuid4())]:
        with pytest.raises(PlanningRunNotFoundError):
            await service.get_snapshot(actor=foreign, draft_id=first.draft_id)
    with pytest.raises(PlanningRunForbiddenError):
        await publish(service, store, replace(actor, role=MembershipRole.EMPLOYEE), 1, "employee")
    store.active = False
    with pytest.raises(PlanningRunForbiddenError):
        await service.get_snapshot(actor=actor, draft_id=first.draft_id)
