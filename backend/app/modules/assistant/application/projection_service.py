"""Idempotent projection of committed focused workflow events into chat."""

from typing import Any
from uuid import UUID

from app.modules.assistant.application.ports import (
    AssistantTransactionFactory,
    LinkedWorkflowEvent,
)
from app.modules.planning_runs.domain.models import WorkflowEvent
from work_management_ai.agents.orchestrator.contracts import PendingFollowup


def advance_pending_followup(
    checkpoint: dict[str, Any], event: WorkflowEvent
) -> tuple[dict[str, Any], bool]:
    """Advance only an exact persisted Project-to-Team continuation."""

    raw = checkpoint.get("pending_followup")
    if not isinstance(raw, dict):
        return dict(checkpoint), False
    try:
        pending = PendingFollowup.model_validate(raw)
    except ValueError:
        return dict(checkpoint), False
    if pending.planning_workflow_run_id != event.workflow_run_id:
        return dict(checkpoint), False
    if event.event_type == "proposal.ready" and pending.state == "WAITING_PROJECT_PROPOSAL":
        try:
            updated = pending.bind_proposal(
                proposal_id=UUID(str(event.public_payload["proposal_id"])),
                proposal_version=int(str(event.public_payload["version"])),
            )
        except (KeyError, TypeError, ValueError):
            return dict(checkpoint), False
        return {
            **checkpoint,
            "pending_followup": updated.model_dump(mode="json"),
        }, False
    if event.event_type != "workflow.completed" or pending.state != "WAITING_PROJECT_DECISION":
        return dict(checkpoint), False
    decision = event.public_payload.get("decision")
    project_id = event.public_payload.get("project_id")
    proposal_id = event.public_payload.get("proposal_id")
    proposal_version = event.public_payload.get("proposal_version")
    if (
        str(pending.planning_proposal_id) != str(proposal_id)
        or pending.planning_proposal_version != proposal_version
    ):
        return dict(checkpoint), False
    updated = (
        pending.mark_ready(project_id=UUID(str(project_id)))
        if decision == "APPROVE" and project_id is not None
        else pending.mark_cancelled()
    )
    return {
        **checkpoint,
        "pending_followup": updated.model_dump(mode="json"),
    }, updated.state == "READY"


class AssistantProjectionService:
    """Map safe committed events without invoking AI or business mutations."""

    def __init__(self, *, transaction_factory: AssistantTransactionFactory) -> None:
        self._transactions = transaction_factory

    async def project_once(self, *, organization_id: UUID, limit: int = 50) -> int:
        bounded = min(max(limit, 1), 50)
        async with self._transactions(organization_id) as transaction:
            items = await transaction.repository.list_unprojected_workflow_events(
                organization_id=organization_id,
                limit=bounded,
            )
            projected = 0
            for item in items:
                blocks, status, safe_error = self._map(item)
                if await transaction.repository.project_workflow_event(
                    item=item,
                    blocks=blocks,
                    status=status,
                    safe_error_code=safe_error,
                ):
                    projected += 1
            projected += await transaction.repository.resume_confirmed_team_followups(
                organization_id=organization_id,
                limit=bounded,
            )
            await transaction.commit()
            return projected

    @staticmethod
    def _map(
        item: LinkedWorkflowEvent,
    ) -> tuple[tuple[dict[str, Any], ...], str | None, str | None]:
        event = item.event
        payload = event.public_payload
        if event.event_type.startswith("planning.draft."):
            return (
                (
                    {
                        "kind": "planning_draft",
                        "workflow_run_id": str(event.workflow_run_id),
                        "draft_id": str(payload["draft_id"]),
                        "draft_version": int(payload["draft_version"]),
                        "summary": str(payload.get("summary", "Planning draft")),
                        "state": payload.get("state", "DRAFT_AVAILABLE"),
                        "conflicts": payload.get("conflicts", []),
                    },
                ),
                "COMPLETED",
                None,
            )
        if event.event_type in {"workflow.needs_input", "manager.input_required"}:
            return (
                (
                    {
                        "kind": "question",
                        "question": str(
                            payload.get(
                                "question",
                                "Additional planning input is required.",
                            )
                        ),
                        "response_context": {
                            "workflow_run_id": str(event.workflow_run_id),
                            **({"progressive": True} if payload.get("progressive") else {}),
                        },
                    },
                ),
                "AWAITING_INPUT",
                None,
            )
        if event.event_type == "proposal.ready":
            return (
                (
                    {
                        "kind": "proposal",
                        "workflow_run_id": str(event.workflow_run_id),
                        "proposal_id": str(payload.get("proposal_id", "")),
                        "proposal_version": int(payload.get("version", 0)),
                        "approval_id": payload.get("approval_id"),
                        "state": "READY_FOR_DECISION",
                        "can_approve": bool(payload.get("can_approve", False)),
                        "read_only": False,
                    },
                ),
                "AWAITING_HUMAN",
                None,
            )
        if event.event_type == "proposal.validation_failed":
            return (
                (
                    {
                        "kind": "proposal",
                        "workflow_run_id": str(event.workflow_run_id),
                        "proposal_id": str(payload.get("proposal_id", "")),
                        "proposal_version": int(payload.get("version", 0)),
                        "state": "VALIDATION_FAILED",
                        "can_approve": False,
                        "read_only": False,
                        "error_codes": list(payload.get("error_codes", [])),
                        "manual_fallback": "PROJECT_TASK_EDITOR",
                    },
                ),
                "AWAITING_HUMAN",
                None,
            )
        if event.event_type in {"proposal.superseded", "proposal.stale"}:
            return (
                (
                    {
                        "kind": "proposal",
                        "workflow_run_id": str(event.workflow_run_id),
                        "proposal_id": str(payload.get("proposal_id", "")),
                        "proposal_version": int(payload.get("base_version", 0)),
                        "current_version": int(payload.get("current_version", 0)),
                        "state": (
                            "SUPERSEDED" if event.event_type.endswith("superseded") else "STALE"
                        ),
                        "read_only": True,
                    },
                ),
                "AWAITING_HUMAN",
                None,
            )
        if event.event_type == "workflow.completed":
            return (
                (
                    {
                        "kind": "decision_result",
                        "workflow_run_id": str(event.workflow_run_id),
                        "decision": str(payload.get("decision", "UNKNOWN")),
                        "proposal_id": str(payload.get("proposal_id", "")),
                        "proposal_version": int(payload.get("proposal_version", 0)),
                        "project_id": payload.get("project_id"),
                        "continue_team": False,
                    },
                ),
                "COMPLETED",
                None,
            )
        if event.event_type in {"workflow.failed", "proposal.revision_failed"}:
            code = str(payload.get("safe_error_code", "AI_WORKFLOW_UNAVAILABLE"))
            return (
                (
                    {
                        "kind": "safe_error",
                        "code": code,
                        "message_key": "ai.error.workflowUnavailable",
                        "manual_fallback": "PROJECT_TASK_EDITOR",
                    },
                ),
                "FAILED" if event.event_type == "workflow.failed" else "AWAITING_HUMAN",
                code if event.event_type == "workflow.failed" else None,
            )
        return (
            (
                {
                    "kind": "activity",
                    "label_key": f"ai.activity.{event.event_type.replace('.', '_')}",
                    "status": "COMPLETED",
                    "agent_id": "planning",
                    "workflow_run_id": str(event.workflow_run_id),
                },
            ),
            None,
            None,
        )
