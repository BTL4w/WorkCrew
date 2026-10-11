"""Restore current authorized draft state before transcript or event delivery."""

from typing import Any, cast
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.planning_runs.adapters.draft_repository import PostgreSQLPlanningDraftRepository
from app.modules.planning_runs.application.draft_service import PlanningDraftService
from work_management_ai.runtime.contracts import PlanningDraftResponseBlock


class PlanningBlockProjector:
    def __init__(
        self, *, drafts: PlanningDraftService, transactions: Any, previous: Any = None
    ) -> None:
        self.drafts, self.transactions, self.previous = drafts, transactions, previous

    async def project(self, actor: AuthenticatedActor, value: Any) -> Any:
        if isinstance(value, list | tuple):
            return [await self.project(actor, item) for item in cast(list[Any], value)]
        if not isinstance(value, dict):
            return value
        value = cast(dict[str, Any], value)
        if value.get("kind") == "proposal":
            try:
                async with self.transactions(actor) as transaction:
                    proposal = await transaction.repository.get_proposal(
                        actor=actor, proposal_id=UUID(str(value["proposal_id"]))
                    )
                    if proposal is None:
                        raise ValueError("proposal unavailable")
                    version = await transaction.repository.get_proposal_version(
                        actor=actor,
                        proposal_id=proposal.id,
                        version_number=int(value["proposal_version"]),
                    )
                    if version is None:
                        raise ValueError("proposal version unavailable")
                    if version.source_draft_id is not None:
                        await PostgreSQLPlanningDraftRepository(transaction).authorize(
                            actor, proposal.workflow_run_id
                        )
                        current = await PostgreSQLPlanningDraftRepository(transaction).get_snapshot(
                            actor, version.source_draft_id, None
                        )
                        stale = (
                            proposal.current_version_number != version.version_number
                            or current.current_version != version.source_draft_version
                            or proposal.status.value != "READY_FOR_DECISION"
                        )
                        value = {
                            **value,
                            "state": "STALE" if stale else "READY_FOR_DECISION",
                            "current_version": proposal.current_version_number,
                            "read_only": stale,
                            "can_approve": not stale and proposal.approval_id is not None,
                        }
            except Exception:
                return {
                    "kind": "safe_error",
                    "code": "PLANNING_CONTEXT_UNAVAILABLE",
                    "message_key": "assistant.draft.unavailable",
                    "manual_fallback": "PROJECT_TASK_EDITOR",
                }
        context = value.get("response_context")
        if value.get("kind") == "planning_draft":
            try:
                card = PlanningDraftResponseBlock.model_validate(value)
                snapshot = await self.drafts.get_snapshot(
                    actor=actor, draft_id=card.draft_id, version=card.draft_version
                )
                if snapshot.workflow_run_id != card.workflow_run_id:
                    raise ValueError("draft workflow mismatch")
                # Preserve historical versions; the client fetches latest to decide editability.
                brief = cast(dict[str, Any], snapshot.content.content.get("brief", {}))
                objective = cast(dict[str, Any], brief.get("objective", {}))
                return card.model_copy(
                    update={"summary": str(objective.get("value", "Planning draft"))}
                ).model_dump(mode="json")
            except Exception:
                return {
                    "kind": "safe_error",
                    "code": "PLANNING_CONTEXT_UNAVAILABLE",
                    "message_key": "assistant.draft.unavailable",
                    "manual_fallback": "PROJECT_TASK_EDITOR",
                }
        if (
            value.get("kind") == "question"
            and isinstance(context, dict)
            and cast(dict[str, Any], context).get("progressive")
        ):
            try:
                async with self.transactions(actor) as transaction:
                    await PostgreSQLPlanningDraftRepository(transaction).authorize(
                        actor, UUID(str(cast(dict[str, Any], context)["workflow_run_id"]))
                    )
            except Exception:
                return {
                    "kind": "safe_error",
                    "code": "PLANNING_CONTEXT_UNAVAILABLE",
                    "message_key": "assistant.draft.unavailable",
                    "manual_fallback": "PROJECT_TASK_EDITOR",
                }
        return await self.previous.project(actor, value) if self.previous else value
