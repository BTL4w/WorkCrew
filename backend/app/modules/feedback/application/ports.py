from contextlib import AbstractAsyncContextManager
from typing import Protocol
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor

from ..domain.feedback import FeedbackCommand, FeedbackResult, TerminalReviewCommand
from ..domain.outcomes import FeedbackOutcome, OutcomeFacts, OutcomeSourceCommand, ReviewRates


class FeedbackSourceReadPort(Protocol):
    async def read_outcome(
        self, actor: AuthenticatedActor, source: OutcomeSourceCommand
    ) -> OutcomeFacts: ...


class FeedbackRepository(Protocol):
    async def rates(self, project_id: UUID) -> ReviewRates: ...
    async def record_outcome(
        self, feedback_id: UUID, source: OutcomeSourceCommand, key: str, fingerprint: str
    ) -> FeedbackOutcome: ...
    async def reject_outcome(
        self, feedback_id: UUID | None, key: str | None, code: str
    ) -> None: ...
    async def record_terminal(self, command: TerminalReviewCommand) -> FeedbackResult: ...
    async def record(
        self, command: FeedbackCommand, key: str, fingerprint: str
    ) -> FeedbackResult: ...
    async def rejected(
        self, command: FeedbackCommand | None, key: str | None, code: str
    ) -> None: ...


class FeedbackTransactionFactory(Protocol):
    def __call__(
        self, actor: AuthenticatedActor
    ) -> AbstractAsyncContextManager[FeedbackRepository]: ...
