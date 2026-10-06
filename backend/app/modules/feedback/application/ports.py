from contextlib import AbstractAsyncContextManager
from typing import Protocol

from app.modules.identity.domain.auth import AuthenticatedActor

from ..domain.feedback import FeedbackCommand, FeedbackResult, TerminalReviewCommand


class FeedbackRepository(Protocol):
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
