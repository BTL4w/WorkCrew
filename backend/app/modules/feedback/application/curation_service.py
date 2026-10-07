"""Admin-only curation through current-authorized transaction ports, without automatic promotion."""

from typing import Any
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.reporting.domain.reports import ReportCaptureConflict, ReportError
from app.modules.reporting.domain.snapshots import canonical_hash

from ..domain.evaluation import (
    CurateCaseCommand,
    DatasetCommand,
    EvaluationCandidate,
    EvaluationCase,
    EvaluationDatasetVersion,
    EvaluationReviewDiff,
)
from .ports import CurationTransactionFactory


class CurationService:
    def __init__(self, transactions: CurationTransactionFactory):
        self.transactions = transactions

    async def preview(
        self, *, actor: AuthenticatedActor, command: CurateCaseCommand, expected_version: int
    ) -> EvaluationReviewDiff:
        async with self.transactions(actor) as repo:
            return await repo.preview(command, expected_version)

    async def prepare(
        self, *, actor: AuthenticatedActor, feedback_id: UUID, idempotency_key: str
    ) -> EvaluationCandidate:
        result = await self._execute(
            actor=actor,
            operation="prepare",
            key=idempotency_key,
            command={"feedback_id": str(feedback_id)},
            feedback_id=feedback_id,
        )
        assert isinstance(result, EvaluationCandidate)
        return result

    async def curate(
        self,
        *,
        actor: AuthenticatedActor,
        command: CurateCaseCommand,
        expected_version: int,
        idempotency_key: str,
    ) -> EvaluationCase:
        result = await self._execute(
            actor=actor,
            operation="curate",
            key=idempotency_key,
            command={"case": command.model_dump(mode="json"), "expected_version": expected_version},
            case=command,
            expected_version=expected_version,
        )
        assert isinstance(result, EvaluationCase)
        return result

    async def freeze_dataset(
        self, *, actor: AuthenticatedActor, command: DatasetCommand, idempotency_key: str
    ) -> EvaluationDatasetVersion:
        result = await self._execute(
            actor=actor,
            operation="freeze",
            key=idempotency_key,
            command=command.model_dump(mode="json"),
            dataset=command,
        )
        assert isinstance(result, EvaluationDatasetVersion)
        return result

    async def _execute(
        self,
        *,
        actor: AuthenticatedActor,
        operation: str,
        key: str,
        command: dict[str, Any],
        feedback_id: UUID | None = None,
        case: CurateCaseCommand | None = None,
        expected_version: int | None = None,
        dataset: DatasetCommand | None = None,
    ) -> EvaluationCandidate | EvaluationCase | EvaluationDatasetVersion:
        try:
            if not 16 <= len(key) <= 128 or (expected_version is not None and expected_version < 1):
                raise ReportError("VALIDATION_FAILED", 422)
            for attempt in range(3):
                try:
                    async with self.transactions(actor) as repo:
                        return await repo.execute(
                            operation,
                            key,
                            canonical_hash(command),
                            feedback_id=feedback_id,
                            case=case,
                            expected_version=expected_version,
                            dataset=dataset,
                        )
                except ReportCaptureConflict:
                    if attempt == 2:
                        raise ReportError("EVALUATION_RETRY") from None
            raise ReportError("EVALUATION_RETRY")
        except ReportError as exc:
            async with self.transactions(actor) as repo:
                await repo.reject_curation(operation, key, exc.code)
            raise
