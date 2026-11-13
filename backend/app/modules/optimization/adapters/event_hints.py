"""Capture actual flushed event/job identities; transaction adapters publish after commit."""

import asyncio

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.modules.assistant.adapters.database_models import AssistantEventModel, AssistantJobModel
from app.modules.optimization.application.wakeup import PendingWakeups, WakeupHint, WakeupPort
from app.modules.planning_runs.adapters.database_models import WorkflowEventModel, WorkflowJobModel


class EventHints:
    def __init__(self, session: AsyncSession, port: WakeupPort | None):
        self._port = port
        self._pending = PendingWakeups()
        if port is not None:
            event.listen(session.sync_session, "after_flush", self._flushed)
            event.listen(session.sync_session, "after_rollback", self._rolled_back)

    def _flushed(self, session: Session, context: object) -> None:
        for row in session.new:
            if isinstance(row, AssistantEventModel):
                self._pending.add(
                    WakeupHint(
                        1,
                        row.organization_id,
                        "assistant.events",
                        row.conversation_id,
                        row.sequence,
                    )
                )
            elif isinstance(row, WorkflowEventModel):
                self._pending.add(
                    WakeupHint(
                        1, row.organization_id, "workflow.events", row.workflow_run_id, row.sequence
                    )
                )

            elif isinstance(row, (AssistantJobModel, WorkflowJobModel)) and row.status == "QUEUED":
                self._pending.add(
                    WakeupHint(1, row.organization_id, "job.ready", row.organization_id, None)
                )

    def _rolled_back(self, session: Session) -> None:
        self.discard()

    def discard(self) -> None:
        self._pending.drain()

    async def committed(self) -> None:
        hints = self._pending.drain()
        if self._port is None:
            return
        try:
            async with asyncio.timeout(0.05):
                for hint in hints:
                    await self._port.publish(hint)
        except Exception:
            # The commit is complete. Hint loss is recovered by source polling.
            return
