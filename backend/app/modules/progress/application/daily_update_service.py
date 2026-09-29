"""One transaction boundary for manual submit and draft confirmation."""

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.progress.domain.daily_updates import (
    ConfirmDailyUpdateCommand,
    ConfirmedDailyUpdate,
    ConfirmedObservation,
    DailyUpdateDraft,
    DailyUpdateError,
    DailyUpdateItemInput,
    TaskReportingContext,
    validate_items,
)


class DailyUpdateRepository(Protocol):
    async def authenticate(self) -> None: ...
    async def draft(self, draft_id: UUID) -> DailyUpdateDraft: ...
    async def save_draft(
        self,
        items: tuple[DailyUpdateItemInput, ...],
        timezone: str,
        draft_id: UUID | None,
        expected_version: int | None,
    ) -> DailyUpdateDraft: ...
    async def replay(
        self, operation: str, key: str, fingerprint: str
    ) -> dict[str, object] | None: ...
    async def remember(
        self, operation: str, key: str, fingerprint: str, result: dict[str, object]
    ) -> None: ...
    async def confirm(self, draft: DailyUpdateDraft, at: datetime) -> ConfirmedDailyUpdate: ...
    async def history(self, task_id: UUID) -> tuple[ConfirmedObservation, ...]: ...
    async def context(self, task_id: UUID, timezone: str, at: datetime) -> TaskReportingContext: ...
    async def audit(
        self,
        action: str,
        request_id: str,
        key: str | None,
        resource_id: UUID | None,
        code: str | None = None,
    ) -> None: ...


class DailyUpdateTransactions(Protocol):
    def __call__(
        self, actor: AuthenticatedActor
    ) -> AbstractAsyncContextManager[DailyUpdateRepository]: ...


class DailyUpdateAssessmentPolicy(Protocol):
    def validate_confirmation(
        self, draft: DailyUpdateDraft, command: ConfirmDailyUpdateCommand
    ) -> None: ...


class UnavailableAssessmentPolicy:
    """Manual fallback policy; an active provider is authorized in Task 7."""

    def validate_confirmation(
        self, draft: DailyUpdateDraft, command: ConfirmDailyUpdateCommand
    ) -> None:
        if (
            draft.assessment_state != "UNAVAILABLE"
            or command.assessment_id
            or command.warning_acknowledgments
        ):
            raise DailyUpdateError("ASSESSMENT_UNAVAILABLE", 422)


class DailyUpdateService:
    def __init__(self, transactions: DailyUpdateTransactions, reporting_timezone: str = "UTC"):
        self.transactions = transactions
        self.reporting_timezone = reporting_timezone
        self.assessment_policy: DailyUpdateAssessmentPolicy = UnavailableAssessmentPolicy()

    async def create_draft(
        self,
        actor: AuthenticatedActor,
        items: tuple[DailyUpdateItemInput, ...],
        key: str,
        request_id: str,
        draft_id: UUID | None = None,
        expected_version: int | None = None,
    ) -> DailyUpdateDraft:
        import hashlib
        import json

        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "items": [i.model_dump(mode="json") for i in items],
                    "draft_id": str(draft_id),
                    "version": expected_version,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        operation = "daily_update.revise" if draft_id else "daily_update.draft"
        try:
            self._key(key)
            async with self.transactions(actor) as repo:
                await repo.authenticate()
                replay = await repo.replay(operation, key, fingerprint)
                if replay is not None:
                    return DailyUpdateDraft.model_validate(replay)
                validate_items(items, datetime.now(UTC), self.reporting_timezone)
                result = await repo.save_draft(
                    items, self.reporting_timezone, draft_id, expected_version
                )
                await repo.remember(operation, key, fingerprint, result.model_dump(mode="json"))
                await repo.audit(operation, request_id, key, result.id)
                return result
        except DailyUpdateError as error:
            await self.reject(actor, request_id, key, error.code, draft_id)
            raise

    async def revise_draft(
        self,
        actor: AuthenticatedActor,
        draft_id: UUID,
        expected_version: int,
        items: tuple[DailyUpdateItemInput, ...],
        key: str,
        request_id: str,
    ) -> DailyUpdateDraft:
        return await self.create_draft(actor, items, key, request_id, draft_id, expected_version)

    async def audit_transport_rejection(
        self,
        *,
        actor: AuthenticatedActor,
        request_id: str,
        key: str | None,
        reason_code: str,
    ) -> None:
        await self.reject(actor, request_id, key, reason_code)

    async def get_draft(self, actor: AuthenticatedActor, draft_id: UUID) -> DailyUpdateDraft:
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            return await repo.draft(draft_id)

    async def confirm(
        self,
        actor: AuthenticatedActor,
        command: ConfirmDailyUpdateCommand,
        key: str,
        request_id: str,
    ) -> ConfirmedDailyUpdate:
        import hashlib

        fingerprint = hashlib.sha256(command.model_dump_json().encode()).hexdigest()
        try:
            self._key(key)
            async with self.transactions(actor) as repo:
                await repo.authenticate()
                replay = await repo.replay("daily_update.confirm", key, fingerprint)
                if replay is not None:
                    return ConfirmedDailyUpdate.model_validate(replay)
                draft = await repo.draft(command.draft_id)
                if draft.version != command.expected_draft_version or draft.confirmed_update_id:
                    raise DailyUpdateError("STALE_DRAFT")
                # Assessment availability is server-owned; client flags cannot turn it into a pass.
                self.assessment_policy.validate_confirmation(draft, command)
                at = datetime.now(UTC)
                validate_items(draft.items, at, draft.reporting_timezone)
                result = await repo.confirm(draft, at)
                await repo.remember(
                    "daily_update.confirm", key, fingerprint, result.model_dump(mode="json")
                )
                await repo.audit("daily_update.confirmed", request_id, key, result.id)
                return result
        except DailyUpdateError as error:
            await self.reject(actor, request_id, key, error.code, command.draft_id)
            raise

    async def history(
        self, actor: AuthenticatedActor, task_id: UUID
    ) -> tuple[ConfirmedObservation, ...]:
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            return await repo.history(task_id)

    async def context(self, actor: AuthenticatedActor, task_id: UUID) -> TaskReportingContext:
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            return await repo.context(task_id, self.reporting_timezone, datetime.now(UTC))

    async def reject(
        self,
        actor: AuthenticatedActor,
        request_id: str,
        key: str | None,
        code: str,
        resource_id: UUID | None = None,
    ) -> None:
        async with self.transactions(actor) as repo:
            await repo.audit(
                "daily_update.rejected",
                request_id,
                key if key and len(key) <= 128 else None,
                resource_id,
                code,
            )

    @staticmethod
    def _key(key: str) -> None:
        if not key or len(key) > 128 or not key.isascii() or any(c.isspace() for c in key):
            raise DailyUpdateError("IDEMPOTENCY_KEY_REQUIRED", 400)
