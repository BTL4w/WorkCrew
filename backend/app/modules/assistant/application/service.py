"""Conversation application service — Task 6.

Owns authorization, normalization, fingerprinting and atomic transaction
boundaries. Never calls a model, provider, Agent Harness or Planning Graph
inline. Posting a message commits exactly one message + turn + orchestration
run + initial event + durable job, then returns a typed 202 result.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from app.modules.assistant.adapters.repository import AssistantDomainLookupError
from app.modules.assistant.application.ports import (
    AssistantConversationMutationResult,
    AssistantConversationSnapshot,
    AssistantTurnMutationResult,
)
from app.modules.assistant.domain.models import (
    AssistantConversation,
    AssistantEvent,
    AssistantIdempotencyKeyReusedError,
    AssistantJob,
    AssistantMessage,
    AssistantTurn,
    MessageRole,
    OrchestrationRun,
)
from app.modules.identity.domain.auth import AuthenticatedActor

# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class AssistantServiceError(Exception):
    """Stable, safe application-level service error."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.safe_message = message


class ResourceNotFoundError(AssistantServiceError):
    """Non-disclosing not-found: hides existence of foreign/other-owner resources."""

    def __init__(self) -> None:
        super().__init__("RESOURCE_NOT_FOUND")


class IdempotencyConflictError(AssistantServiceError):
    """Same key, different payload — cannot replay."""

    def __init__(self) -> None:
        super().__init__("IDEMPOTENCY_KEY_REUSED")


# ---------------------------------------------------------------------------
# PlanningSnapshotPort — read-only hydration only
# ---------------------------------------------------------------------------


class PlanningSnapshotPort(Protocol):
    """Read-only port for hydrating current proposal lifecycle for rendering."""

    async def get_proposal_version(
        self, *, actor: AuthenticatedActor, proposal_id: UUID
    ) -> int | None:
        """Return current proposal version or None if not found."""
        ...


class TeamRecommendationSnapshotPort(Protocol):
    """Read-only port for stale-safe team recommendation card actions."""

    async def get_recommendation_version(
        self, *, actor: AuthenticatedActor, recommendation_id: UUID
    ) -> int | None: ...


# ---------------------------------------------------------------------------
# Fingerprint helpers
# ---------------------------------------------------------------------------


def _canonical(value: Any) -> str:
    """Stable canonical JSON string for fingerprinting."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _fingerprint(*parts: str) -> str:
    return hashlib.sha256(("\x00".join(parts)).encode()).hexdigest()


def _conversation_fingerprint(locale: str, title: str | None) -> str:
    return _fingerprint("conversation.create", locale, title or "")


def _message_fingerprint(
    *,
    conversation_id: UUID,
    message: str,
    locale: str,
    card_action: dict[str, Any] | None,
    if_match_version: int | None,
) -> str:
    canonical_card = _canonical(card_action) if card_action is not None else ""
    version_str = str(if_match_version) if if_match_version is not None else ""
    return _fingerprint(
        "message.submit",
        str(conversation_id),
        message.strip(),
        locale,
        canonical_card,
        version_str,
    )


# ---------------------------------------------------------------------------
# AssistantService
# ---------------------------------------------------------------------------


class AssistantService:
    """Application service for conversation lifecycle and message submission.

    Architecture rules enforced:
    - Never calls model, provider, Agent Harness or Planning Graph.
    - Transaction opened per mutation, committed exactly once after all inserts.
    - Rollback on any exception; safe audit written in a separate transaction.
    - Idempotency: same actor/operation/key + same fingerprint → replay;
      same key + different fingerprint → IdempotencyConflictError.
    - Owner-only visibility: non-disclosing 404 for other owners and tenants.
    """

    def __init__(
        self,
        *,
        transaction_factory: Any,
        planning_snapshot: PlanningSnapshotPort,
        team_recommendation_snapshot: TeamRecommendationSnapshotPort | None = None,
        block_projector: Any = None,
        orchestrator_version: str,
        orchestrator_fingerprint: str,
    ) -> None:
        self._transactions = transaction_factory
        self.block_projector = block_projector
        self._planning_snapshot = planning_snapshot
        self._team_recommendation_snapshot = team_recommendation_snapshot
        self._orchestrator_version = orchestrator_version
        self._orchestrator_fingerprint = orchestrator_fingerprint

    async def _record_rejection(
        self,
        *,
        actor: AuthenticatedActor,
        action: str,
        resource_id: UUID | None,
        request_id: str,
        reason_code: str,
    ) -> None:
        try:
            async with self._transactions(actor) as transaction:
                await transaction.repository.append_rejected_audit(
                    actor=actor,
                    action=action,
                    resource_type="assistant_conversation",
                    resource_id=resource_id,
                    request_id=request_id,
                    reason_code=reason_code,
                )
                await transaction.commit()
        except Exception:
            return

    # ------------------------------------------------------------------
    # create_conversation
    # ------------------------------------------------------------------

    async def create_conversation(
        self,
        *,
        actor: AuthenticatedActor,
        locale: Literal["vi", "en"],
        title: str | None,
        request_id: str,
        idempotency_key: str,
    ) -> AssistantConversationMutationResult:
        normalized_title = title.strip() if title is not None else None
        fingerprint = _conversation_fingerprint(locale, normalized_title)
        conversation = AssistantConversation.create(
            organization_id=actor.organization_id,
            owner_membership_id=actor.membership_id,
            locale=locale,
            title=normalized_title,
        )
        async with self._transactions(actor) as txn:
            try:
                result = await txn.repository.create_conversation_mutation(
                    actor=actor,
                    conversation=conversation,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=fingerprint,
                )
                await txn.commit()
                return result
            except AssistantIdempotencyKeyReusedError as error:
                raise IdempotencyConflictError() from error
            except Exception:
                raise

    async def manage_conversation(
        self,
        *,
        actor: AuthenticatedActor,
        conversation_id: UUID,
        changes: dict[str, object],
        expected_version: int,
        request_id: str,
        idempotency_key: str,
    ) -> AssistantConversationMutationResult:
        allowed = {"title", "is_pinned", "status"}
        if not changes or not changes.keys() <= allowed:
            raise AssistantServiceError("INVALID_REQUEST")
        if "title" in changes:
            title = changes["title"]
            if not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
                raise AssistantServiceError("INVALID_REQUEST")
            changes = {**changes, "title": title.strip()}
        if "is_pinned" in changes and not isinstance(changes["is_pinned"], bool):
            raise AssistantServiceError("INVALID_REQUEST")
        if "status" in changes and changes != {"status": "ARCHIVED"}:
            raise AssistantServiceError("INVALID_REQUEST")
        fingerprint = _fingerprint(str(conversation_id), _canonical(changes), str(expected_version))
        try:
            async with self._transactions(actor) as txn:
                result = await txn.repository.manage_conversation_mutation(
                    actor=actor,
                    conversation_id=conversation_id,
                    changes=changes,
                    expected_version=expected_version,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=fingerprint,
                )
                await txn.commit()
                return result
        except (AssistantDomainLookupError, AssistantIdempotencyKeyReusedError) as error:
            code = str(error)
            await self._record_rejection(
                actor=actor,
                action="assistant.conversation.manage",
                resource_id=conversation_id,
                request_id=request_id,
                reason_code=code,
            )
            if isinstance(error, AssistantIdempotencyKeyReusedError):
                raise IdempotencyConflictError() from error
            if code in {"RESOURCE_VERSION_MISMATCH", "CONVERSATION_BUSY"}:
                raise AssistantServiceError(code) from error
            raise ResourceNotFoundError() from error

    # ------------------------------------------------------------------
    # list_conversations
    # ------------------------------------------------------------------

    async def list_conversations(
        self,
        *,
        actor: AuthenticatedActor,
        limit: int = 50,
    ) -> list[AssistantConversation]:
        async with self._transactions(actor) as txn:
            result = await txn.repository.list_conversations(actor=actor, limit=limit)
            await txn.commit()
            return result

    # ------------------------------------------------------------------
    # get_conversation
    # ------------------------------------------------------------------

    async def get_conversation(
        self,
        *,
        actor: AuthenticatedActor,
        conversation_id: UUID,
    ) -> AssistantConversationSnapshot:
        async with self._transactions(actor) as txn:
            snapshot = await txn.repository.get_conversation_snapshot(
                actor=actor, conversation_id=conversation_id
            )
            await txn.commit()
        if snapshot is None:
            raise ResourceNotFoundError()
        if self.block_projector is not None:
            messages = tuple(
                [
                    replace(
                        m,
                        content_blocks=tuple(
                            await self.block_projector.project(actor, m.content_blocks)
                        ),
                    )
                    for m in snapshot.messages
                ]
            )
            snapshot = replace(snapshot, messages=messages)
        return snapshot

    # ------------------------------------------------------------------
    # post_message
    # ------------------------------------------------------------------

    async def post_message(
        self,
        *,
        actor: AuthenticatedActor,
        conversation_id: UUID,
        message: str,
        locale: Literal["vi", "en"],
        card_action: dict[str, Any] | None,
        if_match_version: int | None,
        request_id: str,
        idempotency_key: str,
    ) -> AssistantTurnMutationResult:
        # Normalize and validate If-Match / card action semantics
        normalized_message = message.strip()
        card_kind = card_action.get("kind") if card_action else None

        # Plain message: If-Match FORBIDDEN
        if card_kind is None and if_match_version is not None:
            raise AssistantServiceError("IF_MATCH_FORBIDDEN")

        # PLANNING_REVISE: proposal_id and If-Match both REQUIRED
        if card_kind == "PLANNING_REVISE":
            if not card_action or not card_action.get("proposal_id"):
                raise AssistantServiceError("PROPOSAL_ID_REQUIRED")
            if if_match_version is None:
                raise AssistantServiceError("IF_MATCH_REQUIRED")
            proposal_id = UUID(str(card_action["proposal_id"]))
            current_version = await self._planning_snapshot.get_proposal_version(
                actor=actor,
                proposal_id=proposal_id,
            )
            if current_version is None:
                raise ResourceNotFoundError()
            if current_version != if_match_version:
                raise AssistantServiceError("RESOURCE_VERSION_MISMATCH")

        if card_kind == "TEAM_REVISE":
            if not card_action or not card_action.get("recommendation_id"):
                raise AssistantServiceError("RECOMMENDATION_ID_REQUIRED")
            if if_match_version is None:
                raise AssistantServiceError("IF_MATCH_REQUIRED")
            if self._team_recommendation_snapshot is None:
                raise AssistantServiceError("ASSISTANT_UNAVAILABLE")
            if card_action.get("recommendation_version") != if_match_version:
                raise AssistantServiceError("RESOURCE_VERSION_MISMATCH")
            recommendation_id = UUID(str(card_action["recommendation_id"]))
            current_version = await self._team_recommendation_snapshot.get_recommendation_version(
                actor=actor,
                recommendation_id=recommendation_id,
            )
            if current_version is None:
                raise ResourceNotFoundError()
            if current_version != if_match_version:
                raise AssistantServiceError("RESOURCE_VERSION_MISMATCH")

        fingerprint = _message_fingerprint(
            conversation_id=conversation_id,
            message=normalized_message,
            locale=locale,
            card_action=card_action,
            if_match_version=if_match_version,
        )

        # Build domain objects (IDs minted now; conversation lock happens in repository)
        message_id = uuid4()
        turn_id = uuid4()
        run_id = uuid4()
        job_id = uuid4()
        event_id = uuid4()

        content_blocks: tuple[dict[str, Any], ...] = ({"kind": "text", "text": normalized_message},)
        if card_kind is not None:
            trusted_action = dict(card_action or {})
            if if_match_version is not None:
                trusted_action["expected_version"] = if_match_version
            content_blocks = (
                *content_blocks,
                {"kind": "accepted_card_action", "action": trusted_action},
            )
        user_message = AssistantMessage(
            id=message_id,
            organization_id=actor.organization_id,
            conversation_id=conversation_id,
            sequence=1,  # Repository enforces correct sequence via conversation lock
            role=MessageRole.USER,
            content_blocks=content_blocks,
            created_by_membership_id=actor.membership_id,
            turn_id=turn_id,
            dedupe_key=f"user:{idempotency_key}",
        )

        turn = AssistantTurn.create(
            organization_id=actor.organization_id,
            conversation_id=conversation_id,
            user_message_id=message_id,
            actor_membership_id=actor.membership_id,
            objective=normalized_message,
            locale=locale,
            id=turn_id,
        )

        run = OrchestrationRun.create(
            organization_id=actor.organization_id,
            turn_id=turn_id,
            orchestrator_version=self._orchestrator_version,
            orchestrator_fingerprint=self._orchestrator_fingerprint,
            execution_plan={"steps": [], "schema_version": "1.0"},
            budget={"max_iterations": 8, "timeout_seconds": 120},
            id=run_id,
        )

        job = AssistantJob.create(
            organization_id=actor.organization_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            orchestration_run_id=run_id,
            requester_membership_id=actor.membership_id,
            payload={
                "conversation_id": str(conversation_id),
                "turn_id": str(turn_id),
                "orchestration_run_id": str(run_id),
                "locale": locale,
                "idempotency_key": idempotency_key,
            },
            id=job_id,
        )

        initial_event = AssistantEvent(
            id=event_id,
            organization_id=actor.organization_id,
            conversation_id=conversation_id,
            sequence=1,  # Repository enforces correct sequence
            event_type="assistant.turn.queued.v1",
            public_payload={
                "turn_id": str(turn_id),
                "orchestration_run_id": str(run_id),
                "status": "QUEUED",
            },
            turn_id=turn_id,
            orchestration_run_id=run_id,
            dedupe_key=f"turn.queued:{turn_id}",
        )

        try:
            async with self._transactions(actor) as txn:
                result = await txn.repository.submit_message_mutation(
                    actor=actor,
                    message=user_message,
                    turn=turn,
                    run=run,
                    job=job,
                    event=initial_event,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=fingerprint,
                )
                await txn.commit()
                return result
        except AssistantIdempotencyKeyReusedError as error:
            await self._record_rejection(
                actor=actor,
                action="assistant.message.submit",
                resource_id=conversation_id,
                request_id=request_id,
                reason_code="IDEMPOTENCY_KEY_REUSED",
            )
            raise IdempotencyConflictError() from error
        except AssistantDomainLookupError as error:
            await self._record_rejection(
                actor=actor,
                action="assistant.message.submit",
                resource_id=conversation_id,
                request_id=request_id,
                reason_code="RESOURCE_NOT_FOUND",
            )
            raise ResourceNotFoundError() from error
        except AssistantServiceError:
            raise
        except Exception as error:
            await self._record_rejection(
                actor=actor,
                action="assistant.message.submit",
                resource_id=conversation_id,
                request_id=request_id,
                reason_code="ASSISTANT_UNAVAILABLE",
            )
            raise AssistantServiceError("ASSISTANT_UNAVAILABLE") from error
