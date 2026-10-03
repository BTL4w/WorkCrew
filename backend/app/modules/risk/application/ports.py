"""Typed ports owned by the risk application, without provider/SQL dependencies."""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.risk.domain.assessments import (
    RiskAssessment,
    RiskInputs,
    RiskJudgment,
    RiskReviewCommand,
    RiskReviewEvent,
    WeeklyRisk,
)
from app.modules.risk.domain.notifications import RiskNotification


@dataclass(frozen=True)
class RiskLease:
    id: UUID
    task_id: UUID
    cause_id: UUID
    attempt: int
    inputs: RiskInputs


class RiskAssessmentPort(Protocol):
    async def assess(self, inputs: RiskInputs) -> tuple[RiskJudgment, str]: ...


class RiskRepositoryPort(Protocol):
    async def authenticate(self) -> None: ...
    async def authorize(self, task_id: UUID) -> None: ...
    async def weekly(self, week_id: UUID) -> WeeklyRisk: ...
    async def get(self, risk_id: UUID) -> RiskAssessment: ...
    async def replay(
        self, operation: str, key: str, fingerprint: str
    ) -> dict[str, object] | None: ...
    async def remember(
        self, operation: str, key: str, fingerprint: str, result: dict[str, object]
    ) -> None: ...
    async def enqueue(self, task_id: UUID, cause_id: UUID) -> RiskAssessment: ...
    async def current(self, task_id: UUID) -> RiskAssessment | None: ...
    async def claim(self) -> RiskLease | None: ...
    async def finish(self, lease: RiskLease, result: RiskAssessment) -> None: ...
    async def reviews(self, risk_id: UUID) -> tuple[RiskReviewEvent, ...]: ...
    async def review(
        self, risk_id: UUID, command: RiskReviewCommand, key: str
    ) -> RiskReviewEvent: ...
    async def notifications(self) -> tuple[RiskNotification, ...]: ...
    async def notify(self, risk_id: UUID) -> None: ...
    async def read_notification(self, id: UUID, key: str) -> RiskNotification: ...
    async def audit(
        self,
        action: str,
        request_id: str,
        key: str | None,
        resource_id: UUID | None,
        code: str | None = None,
    ) -> None: ...


class RiskTransactionPort(Protocol):
    def __call__(
        self, actor: AuthenticatedActor
    ) -> AbstractAsyncContextManager[RiskRepositoryPort]: ...
