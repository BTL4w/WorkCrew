from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor

from ..domain.evaluation import (
    CurateCaseCommand,
    DatasetCommand,
    EvaluationCandidate,
    EvaluationCase,
    EvaluationDatasetVersion,
    EvaluationReviewDiff,
    ReportEvaluationResult,
)
from ..domain.evaluation_runs import EvaluationRequest, EvaluationRun
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


class CurationRepository(Protocol):
    async def preview(
        self, command: CurateCaseCommand, expected_version: int
    ) -> EvaluationReviewDiff: ...

    async def execute(
        self,
        operation: str,
        key: str,
        fingerprint: str,
        *,
        feedback_id: UUID | None = None,
        case: CurateCaseCommand | None = None,
        expected_version: int | None = None,
        dataset: DatasetCommand | None = None,
    ) -> EvaluationCandidate | EvaluationCase | EvaluationDatasetVersion: ...
    async def reject_curation(self, operation: str, key: str, code: str) -> None: ...


class CurationTransactionFactory(Protocol):
    def __call__(
        self, actor: AuthenticatedActor
    ) -> AbstractAsyncContextManager[CurationRepository]: ...


class EvaluationDatasetReadPort(Protocol):
    async def load(self, actor: AuthenticatedActor, identity: UUID) -> EvaluationDatasetVersion: ...


class EvaluationProviderPort(Protocol):
    async def evaluate(self, dataset: EvaluationDatasetVersion) -> ReportEvaluationResult: ...


class EvaluationRepository(Protocol):
    async def authenticate(self) -> None: ...
    async def frozen(self, dataset_id: UUID) -> EvaluationDatasetVersion: ...
    async def replay_run(self, request: EvaluationRequest, key: str) -> EvaluationRun | None: ...
    async def start(
        self, request: EvaluationRequest, key: str, config_hash: str, budget: int
    ) -> EvaluationRun: ...
    async def read(self, identity: UUID) -> EvaluationRun: ...
    async def claim(self, worker: str) -> EvaluationRun | None: ...
    async def fence(self, job: EvaluationRun) -> object: ...
    async def heartbeat(self, job: EvaluationRun) -> None: ...
    async def finish(
        self,
        job: EvaluationRun,
        result: ReportEvaluationResult | None,
        *,
        failure: str | None = None,
        code: str | None = None,
    ) -> None: ...
    async def evidence(
        self, operation: str, key: str, identity: UUID | None, code: str | None = None
    ) -> None: ...


class EvaluationTransactionsPort(Protocol):
    def __call__(
        self, actor: AuthenticatedActor | UUID
    ) -> AbstractAsyncContextManager[EvaluationRepository]: ...
    async def resolve(
        self, *, organization_id: UUID, membership_id: UUID
    ) -> AuthenticatedActor | None: ...


class EvaluationPolicyPort(Protocol):
    budget: int

    def fingerprint(self, provider: str) -> str: ...
    def validate(self, provider: str) -> None: ...
    def build(
        self, job: EvaluationRun, authorize: Callable[[], Awaitable[None]]
    ) -> EvaluationProviderPort: ...

    def verify(
        self, job: EvaluationRun, dataset: EvaluationDatasetVersion, result: ReportEvaluationResult
    ) -> ReportEvaluationResult: ...
