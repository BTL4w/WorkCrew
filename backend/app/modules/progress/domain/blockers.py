"""Versioned manual blocker lifecycle with immutable transition values."""

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from app.modules.progress.domain.reporting_contracts import ReportingContract, SelectedEvidence

BlockerStatus = Literal["OPEN", "ACKNOWLEDGED", "RESOLVED"]
BlockerAction = Literal["CREATE", "EDIT", "ACKNOWLEDGE", "RESOLVE", "REOPEN", "ARCHIVE"]
Severity = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]


class BlockerError(Exception):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code, self.status = code, status


class BlockerCommand(ReportingContract):
    task_id: UUID
    expected_task_version: int = Field(ge=1)
    severity: Severity = "MEDIUM"
    text: str = Field(default="", max_length=4000)
    evidence_refs: tuple[SelectedEvidence, ...] = Field(default=(), max_length=20)
    expected_blocker_version: int | None = Field(default=None, ge=1)
    blocker_id: UUID | None = None
    action: BlockerAction

    @model_validator(mode="after")
    def validate_action(self) -> "BlockerCommand":
        if self.action == "CREATE":
            if self.blocker_id or self.expected_blocker_version:
                raise ValueError("create cannot specify an existing blocker")
        elif self.blocker_id is None or self.expected_blocker_version is None:
            raise ValueError("mutation requires exact blocker version")
        if self.action in {"CREATE", "EDIT"} and not self.text.strip():
            raise ValueError("blocker text is required")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("duplicate evidence")
        return self


class Blocker(ReportingContract):
    id: UUID
    task_id: UUID
    created_by_membership_id: UUID
    severity: Severity
    text: str
    evidence_refs: tuple[SelectedEvidence, ...] = ()
    status: BlockerStatus
    archived: bool = False
    version: int
    created_at: datetime
    updated_at: datetime

    @property
    def severe(self) -> bool:
        return self.severity in {"HIGH", "CRITICAL"}


class BlockerTransition(ReportingContract):
    id: UUID
    blocker_id: UUID
    version: int
    actor_membership_id: UUID
    action: BlockerAction
    from_status: BlockerStatus | None
    to_status: BlockerStatus
    at: datetime
    snapshot: Blocker


def transition_blocker(
    current: Blocker | None, command: BlockerCommand, actor_id: UUID, at: datetime
) -> tuple[Blocker, BlockerTransition]:
    if current is None:
        if command.action != "CREATE":
            raise BlockerError("RESOURCE_NOT_FOUND", 404)
        result = Blocker(
            id=uuid4(),
            task_id=command.task_id,
            created_by_membership_id=actor_id,
            severity=command.severity,
            text=command.text.strip(),
            evidence_refs=command.evidence_refs,
            status="OPEN",
            version=1,
            created_at=at,
            updated_at=at,
        )
    else:
        if (
            current.id != command.blocker_id
            or current.task_id != command.task_id
            or current.version != command.expected_blocker_version
        ):
            raise BlockerError("STALE_BLOCKER")
        if current.archived:
            raise BlockerError("BLOCKER_ARCHIVED")
        allowed = {
            "ACKNOWLEDGE": {"OPEN"},
            "RESOLVE": {"OPEN", "ACKNOWLEDGED"},
            "REOPEN": {"RESOLVED"},
            "EDIT": {"OPEN", "ACKNOWLEDGED", "RESOLVED"},
            "ARCHIVE": {"OPEN", "ACKNOWLEDGED", "RESOLVED"},
        }
        if current.status not in allowed.get(command.action, set()):
            raise BlockerError("INVALID_BLOCKER_TRANSITION", 422)
        status: BlockerStatus = current.status
        if command.action == "ACKNOWLEDGE":
            status = "ACKNOWLEDGED"
        elif command.action in {"RESOLVE", "ARCHIVE"}:
            status = "RESOLVED"
        elif command.action == "REOPEN":
            status = "OPEN"
        changes: dict[str, object] = {
            "version": current.version + 1,
            "status": status,
            "updated_at": at,
            "archived": command.action == "ARCHIVE",
        }
        if command.action == "EDIT":
            changes.update(
                text=command.text.strip(),
                severity=command.severity,
                evidence_refs=command.evidence_refs,
            )
        result = Blocker.model_validate({**current.model_dump(), **changes})
    return result, BlockerTransition(
        id=uuid4(),
        blocker_id=result.id,
        version=result.version,
        actor_membership_id=actor_id,
        action=command.action,
        from_status=current.status if current else None,
        to_status=result.status,
        at=at,
        snapshot=result,
    )
