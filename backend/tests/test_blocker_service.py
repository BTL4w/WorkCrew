from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.modules.progress.domain.blockers import (
    Blocker,
    BlockerAction,
    BlockerCommand,
    BlockerError,
    BlockerTransition,
    transition_blocker,
)


def command(task: UUID, action: BlockerAction, blocker: Blocker | None = None) -> BlockerCommand:
    return BlockerCommand(
        task_id=task,
        expected_task_version=1,
        severity="HIGH",
        text="Awaiting materials",
        action=action,
        blocker_id=blocker.id if blocker else None,
        expected_blocker_version=blocker.version if blocker else None,
    )


def test_blocker_reopen_preserves_lifecycle():
    task, actor = uuid4(), uuid4()
    history: list[BlockerTransition] = []
    current = None
    for action in ("CREATE", "ACKNOWLEDGE", "RESOLVE", "REOPEN"):
        current, event = transition_blocker(
            current, command(task, action, current), actor, datetime.now(UTC)
        )
        history.append(event)
    assert current is not None
    assert current.status == "OPEN"
    assert [event.to_status for event in history] == ["OPEN", "ACKNOWLEDGED", "RESOLVED", "OPEN"]
    assert current.version == 4
    assert current.severe is True


def test_stale_version_and_wrong_task_rejected():
    task, actor = uuid4(), uuid4()
    current, _ = transition_blocker(None, command(task, "CREATE"), actor, datetime.now(UTC))
    for change in ({"expected_blocker_version": 99}, {"task_id": uuid4()}):
        with pytest.raises(BlockerError):
            transition_blocker(
                current,
                command(task, "ACKNOWLEDGE", current).model_copy(update=change),
                actor,
                datetime.now(UTC),
            )


def test_archive_preserves_metadata_and_prevents_reopen():
    task, actor = uuid4(), uuid4()
    original, _ = transition_blocker(None, command(task, "CREATE"), actor, datetime.now(UTC))
    archived, _ = transition_blocker(
        original, command(task, "ARCHIVE", original), actor, datetime.now(UTC)
    )
    assert archived.status == "RESOLVED" and archived.archived
    assert archived.text == original.text
    with pytest.raises(BlockerError):
        transition_blocker(archived, command(task, "REOPEN", archived), actor, datetime.now(UTC))
