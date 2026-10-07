"""Human-reviewed fixtures only; replay rechecks current source authorization."""

from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, text

from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.audit.domain.events import AuditOutcome
from app.modules.planning_runs.adapters.database_models import OutboxEventModel
from app.modules.reporting.domain.reports import ReportError, ReportResult
from app.modules.work.adapters.database_models import IdempotencyRecordModel, IdempotencyState

from ..domain.evaluation import (
    DATASET_POLICY,
    CurateCaseCommand,
    DatasetCommand,
    EvaluationCandidate,
    EvaluationCase,
    EvaluationDatasetVersion,
    EvaluationPayload,
    EvaluationReviewDiff,
    dataset_hash,
    normalized_case_hash,
)
from .database_models import FeedbackModel
from .evaluation_models import EvaluationCandidateModel as CandidateRow
from .evaluation_models import EvaluationCaseModel as CaseRow
from .evaluation_models import EvaluationCaseRevisionModel as RevisionRow
from .evaluation_models import EvaluationDatasetCaseModel as MemberRow
from .evaluation_models import EvaluationDatasetVersionModel as DatasetRow
from .repository import SQLFeedbackRepository


class SQLCurationRepository(SQLFeedbackRepository):
    async def authenticate(self) -> None:
        await super().authenticate()
        role = await self.session.scalar(
            text("SELECT role FROM memberships WHERE organization_id=:org AND id=:member"),
            {"org": self.org, "member": self.actor.membership_id},
        )
        if role != "ADMIN":
            raise ReportError("FORBIDDEN", 403)

    async def source(
        self, feedback_id: UUID, outcome_ids: tuple[UUID, ...] = ()
    ) -> tuple[FeedbackModel, ReportResult]:
        feedback = await self.session.scalar(
            select(FeedbackModel).where(
                FeedbackModel.organization_id == self.org, FeedbackModel.id == feedback_id
            )
        )
        if feedback is None:
            raise ReportError("RESOURCE_NOT_FOUND", 404)
        result = await self.get(feedback.report_id, version_id=feedback.report_version_id)
        if (
            feedback.kind != "TERMINAL_QUALITY"
            or feedback.decision not in ("ACCEPT", "EDIT", "REJECT")
            or (feedback.decision in ("EDIT", "REJECT") and not (feedback.reason or "").strip())
        ):
            raise ReportError("EVALUATION_REASON_REQUIRED")
        job, original = await self.lineage(result.selected_version)
        if (
            feedback.generation_id != job.id
            or feedback.original_version_id != original.id
            or feedback.snapshot_hash != result.snapshot.snapshot_hash
        ):
            raise ReportError("REPORT_AI_LINEAGE_REQUIRED")
        visible = {
            outcome.id for outcome in result.feedback_outcomes if outcome.feedback_id == feedback_id
        }
        if not set(outcome_ids).issubset(visible):
            raise ReportError("RESOURCE_NOT_FOUND", 404)
        return feedback, result

    async def candidate_source(self, candidate_id: UUID) -> CandidateRow:
        row = await self.session.scalar(
            select(CandidateRow).where(
                CandidateRow.organization_id == self.org, CandidateRow.id == candidate_id
            )
        )
        if row is None:
            raise ReportError("RESOURCE_NOT_FOUND", 404)
        await self.source(row.feedback_id, tuple(UUID(item) for item in row.source_outcome_ids))
        return row

    async def candidate(self, candidate_id: UUID) -> EvaluationCandidate:
        row = await self.candidate_source(candidate_id)
        if row.context is None or row.expires_at <= await self.captured_at():
            raise ReportError("EVALUATION_CONTEXT_EXPIRED")
        value = EvaluationCandidate.model_validate(row.context).model_copy(
            update={"version": row.version, "status": row.status}
        )
        await self.source(value.feedback_id, value.outcome_ids)
        return value

    async def reviewed_case(self, case_id: UUID, version: int) -> EvaluationCase:
        row = await self.session.scalar(
            select(RevisionRow).where(
                RevisionRow.organization_id == self.org,
                RevisionRow.case_id == case_id,
                RevisionRow.version == version,
            )
        )
        if row is None:
            raise ReportError("RESOURCE_NOT_FOUND", 404)
        value = EvaluationCase.model_validate(row.body)
        outcomes = value.provenance.get("outcome_ids", [])
        if not isinstance(outcomes, list) or not all(isinstance(item, str) for item in outcomes):
            raise ReportError("EVALUATION_INTEGRITY_FAILED")
        await self.source(value.feedback_id, tuple(UUID(str(item)) for item in outcomes))
        if value.case_hash != normalized_case_hash(value.payload, value.expected_assertions):
            raise ReportError("EVALUATION_INTEGRITY_FAILED")
        return value

    async def frozen(self, dataset_id: UUID) -> EvaluationDatasetVersion:
        row = await self.session.scalar(
            select(DatasetRow).where(
                DatasetRow.organization_id == self.org, DatasetRow.id == dataset_id
            )
        )
        if row is None:
            raise ReportError("RESOURCE_NOT_FOUND", 404)
        value = EvaluationDatasetVersion.model_validate(row.manifest)
        if not value.verified_hash():
            raise ReportError("EVALUATION_INTEGRITY_FAILED")
        for case in value.cases:
            if await self.reviewed_case(case.id, case.version) != case:
                raise ReportError("EVALUATION_INTEGRITY_FAILED")
        return value

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
    ) -> EvaluationCandidate | EvaluationCase | EvaluationDatasetVersion:
        await self.authenticate()
        # Serialize tenant-local hash/version allocation, including competing idempotency keys.
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"{self.org}:evaluation-curation"},
        )
        replay = await self.session.scalar(
            select(IdempotencyRecordModel).where(
                IdempotencyRecordModel.organization_id == self.org,
                IdempotencyRecordModel.actor_membership_id == self.actor.membership_id,
                IdempotencyRecordModel.operation == f"evaluation.{operation}",
                IdempotencyRecordModel.idempotency_key == key,
            )
        )
        if replay:
            if replay.request_fingerprint != fingerprint or replay.response_body is None:
                raise ReportError("IDEMPOTENCY_KEY_REUSED")
            # Reauthorize both sources when deduplication returned another source's case.
            if feedback_id is not None:
                await self.source(feedback_id)
            if case is not None:
                await self.candidate_source(case.candidate_id)
            pointer = replay.response_body
            identity = UUID(str(pointer["id"]))
            if operation == "prepare":
                value = await self.candidate(identity)
                return EvaluationCandidate.model_validate(
                    {
                        **value.model_dump(mode="python"),
                        "version": pointer["version"],
                        "status": pointer["status"],
                    }
                )
            if operation == "curate":
                return await self.reviewed_case(identity, int(pointer["version"]))
            return await self.frozen(identity)
        if operation == "prepare" and feedback_id is not None:
            result = await self.prepare(feedback_id)
        elif operation == "curate" and case is not None and expected_version is not None:
            result = await self.curate(case, expected_version)
        elif operation == "freeze" and dataset is not None:
            result = await self.freeze(dataset)
        else:
            raise ReportError("VALIDATION_FAILED", 422)
        at = await self.captured_at()
        self.session.add(
            IdempotencyRecordModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id,
                operation=f"evaluation.{operation}",
                idempotency_key=key,
                request_fingerprint=fingerprint,
                state=IdempotencyState.COMPLETED,
                response_status=201,
                response_body={
                    "id": str(result.id),
                    "version": result.version,
                    **(
                        {"status": result.status} if isinstance(result, EvaluationCandidate) else {}
                    ),
                },
                expires_at=at + timedelta(days=7),
            )
        )
        await self.evidence(operation, key, result.id)
        await self.session.flush()
        return result

    async def prepare(self, feedback_id: UUID) -> EvaluationCandidate:
        feedback, result = await self.source(feedback_id)
        existing = await self.session.scalar(
            select(CandidateRow).where(
                CandidateRow.organization_id == self.org, CandidateRow.feedback_id == feedback_id
            )
        )
        if existing:
            return await self.candidate(existing.id)
        at = await self.captured_at()
        # Numeric snapshot projection deliberately excludes evidence text and personal identifiers.
        metrics = {
            key: value.model_copy(update={"source_refs": (), "limitations": ()})
            for key, value in result.snapshot.metrics.items()
            if value.policy_version == "report-metrics.v1"
        }
        value = EvaluationCandidate(
            id=uuid4(),
            feedback_id=feedback_id,
            generation_id=feedback.generation_id,
            original_version_id=feedback.original_version_id,
            report_version_id=feedback.report_version_id,
            snapshot_id=result.snapshot.id,
            snapshot_hash=result.snapshot.snapshot_hash,
            version=1,
            status="PENDING_REVIEW",
            payload=EvaluationPayload(
                locale=result.report.locale,
                period_kind="DAILY" if result.report.kind.value == "DAILY" else "WEEKLY",
                metrics=metrics,
            ),
            outcome_ids=tuple(
                outcome.id
                for outcome in result.feedback_outcomes
                if outcome.feedback_id == feedback_id
            ),
            provenance={
                "organization_id": str(self.org),
                "feedback_id": str(feedback_id),
                "generation_id": str(feedback.generation_id),
                "original_version_id": str(feedback.original_version_id),
                "report_version_id": str(feedback.report_version_id),
                "snapshot_id": str(result.snapshot.id),
                "snapshot_hash": result.snapshot.snapshot_hash,
                "generation_versions": {
                    key: feedback.provenance[key]
                    for key in (
                        "schema_version",
                        "agent_id",
                        "agent_version",
                        "manifest_version",
                        "workflow_version",
                        "prompt_version",
                        "grounding_prompt_version",
                        "skill_version",
                        "tool_version",
                        "model_version",
                        "verifier_version",
                        "numeric_verifier_version",
                        "semantic_verifier_version",
                        "manifest_fingerprint",
                        "skill_versions",
                        "tool_versions",
                        "model_refs",
                        "orchestration_run_id",
                    )
                    if key in feedback.provenance
                },
            },
            created_at=at,
            expires_at=at + timedelta(days=30),
        )
        self.session.add(
            CandidateRow(
                id=value.id,
                organization_id=self.org,
                feedback_id=feedback_id,
                version=1,
                status=value.status,
                payload_classification=value.payload_classification,
                context=value.model_dump(mode="json"),
                source_outcome_ids=[str(item) for item in value.outcome_ids],
                created_at=at,
                expires_at=value.expires_at,
            )
        )
        return value

    @staticmethod
    def validate_payload(candidate: EvaluationCandidate, command: CurateCaseCommand) -> None:
        assert candidate.payload is not None
        if command.payload.period_kind != candidate.payload.period_kind:
            raise ReportError("EVALUATION_SOURCE_MISMATCH")
        for key, metric in command.payload.metrics.items():
            original = candidate.payload.metrics.get(key)
            if original is None or (original.unit, original.time_basis) != (
                metric.unit,
                metric.time_basis,
            ):
                raise ReportError("EVALUATION_SOURCE_MISMATCH")
            if command.origin == "REDACTED" and original != metric:
                raise ReportError("EVALUATION_SOURCE_MISMATCH")

    async def preview(
        self, command: CurateCaseCommand, expected_version: int
    ) -> EvaluationReviewDiff:
        await self.authenticate()
        candidate = await self.candidate(command.candidate_id)
        if candidate.version != expected_version:
            raise ReportError("STALE_VERSION")
        self.validate_payload(candidate, command)
        try:
            command.validate_assertions()
        except ValueError as exc:
            raise ReportError(str(exc), 422) from None
        assert candidate.payload is not None
        return EvaluationReviewDiff(
            candidate_id=candidate.id,
            candidate_version=candidate.version,
            before=candidate.payload,
            after=command.payload,
            expected_assertions=command.expected_assertions,
            origin=command.origin,
            split=command.split,
            case_hash=normalized_case_hash(command.payload, command.expected_assertions),
        )

    async def curate(self, command: CurateCaseCommand, expected_version: int) -> EvaluationCase:
        candidate = await self.candidate(command.candidate_id)
        if candidate.version != expected_version:
            raise ReportError("STALE_VERSION")
        try:
            command.validate_review()
        except ValueError as exc:
            raise ReportError(str(exc), 422) from None
        self.validate_payload(candidate, command)
        digest = normalized_case_hash(command.payload, command.expected_assertions)
        duplicate = await self.session.scalar(
            select(RevisionRow).where(
                RevisionRow.organization_id == self.org, RevisionRow.case_hash == digest
            )
        )
        root = await self.session.scalar(
            select(CaseRow).where(
                CaseRow.organization_id == self.org,
                CaseRow.candidate_id == candidate.id,
                CaseRow.locale == command.payload.locale,
            )
        )
        if (duplicate and duplicate.split != command.split) or (
            root and root.split != command.split
        ):
            raise ReportError("EVALUATION_SPLIT_CONFLICT")
        if duplicate:
            if duplicate.origin != command.origin:
                raise ReportError("EVALUATION_ORIGIN_CONFLICT")
            result = await self.reviewed_case(duplicate.case_id, duplicate.version)
        else:
            if root is None:
                root = CaseRow(
                    id=uuid4(),
                    organization_id=self.org,
                    candidate_id=candidate.id,
                    locale=command.payload.locale,
                    split=command.split,
                    current_version=1,
                )
                self.session.add(root)
                await self.session.flush()
            else:
                root.current_version += 1
            at = await self.captured_at()
            result = EvaluationCase(
                id=root.id,
                revision_id=uuid4(),
                candidate_id=candidate.id,
                feedback_id=candidate.feedback_id,
                version=root.current_version,
                origin=command.origin,
                split=command.split,
                case_hash=digest,
                payload=command.payload,
                expected_assertions=command.expected_assertions,
                curator_membership_id=self.actor.membership_id,
                provenance={
                    **candidate.provenance,
                    "candidate_version": candidate.version,
                    "outcome_ids": [str(item) for item in candidate.outcome_ids],
                },
                created_at=at,
            )
            self.session.add(
                RevisionRow(
                    id=result.revision_id,
                    organization_id=self.org,
                    case_id=result.id,
                    version=result.version,
                    case_hash=digest,
                    origin=result.origin,
                    split=result.split,
                    policy_version=DATASET_POLICY,
                    curator_membership_id=result.curator_membership_id,
                    body=result.model_dump(mode="json"),
                    created_at=at,
                )
            )
        row = await self.session.get(CandidateRow, candidate.id)
        assert row is not None
        row.version += 1
        row.status = "DUPLICATE" if duplicate else "CURATED"
        return result

    async def freeze(self, command: DatasetCommand) -> EvaluationDatasetVersion:
        if command.cases:
            cases = tuple(
                [await self.reviewed_case(item.case_id, item.version) for item in command.cases]
            )
        else:
            rows = await self.session.scalars(
                select(RevisionRow)
                .join(
                    CaseRow,
                    (CaseRow.organization_id == RevisionRow.organization_id)
                    & (CaseRow.id == RevisionRow.case_id)
                    & (CaseRow.current_version == RevisionRow.version),
                )
                .where(
                    RevisionRow.organization_id == self.org,
                    RevisionRow.split == command.split,
                    RevisionRow.origin.in_(
                        ("SYNTHETIC", "REDACTED") if command.include_redacted else ("SYNTHETIC",)
                    ),
                )
                .order_by(RevisionRow.case_id)
                .limit(101)
            )
            cases = tuple([await self.reviewed_case(row.case_id, row.version) for row in rows])
        if not cases or len(cases) > 100:
            raise ReportError("EVALUATION_DATASET_EMPTY_OR_TOO_LARGE")
        if any(
            case.split != command.split
            or (case.origin != "SYNTHETIC" and not command.include_redacted)
            for case in cases
        ):
            raise ReportError("EVALUATION_DATASET_SELECTION_INVALID")
        if len({case.id for case in cases}) != len(cases) or len(
            {case.case_hash for case in cases}
        ) != len(cases):
            raise ReportError("EVALUATION_DUPLICATE_CASE")
        cases = tuple(sorted(cases, key=lambda item: (str(item.id), item.version)))
        digest = dataset_hash(command.name, command.split, cases)
        existing = await self.session.scalar(
            select(DatasetRow).where(
                DatasetRow.organization_id == self.org,
                DatasetRow.name == command.name,
                DatasetRow.split == command.split,
                DatasetRow.dataset_hash == digest,
            )
        )
        if existing:
            return await self.frozen(existing.id)
        latest = await self.session.scalar(
            select(func.max(DatasetRow.version)).where(
                DatasetRow.organization_id == self.org,
                DatasetRow.name == command.name,
                DatasetRow.split == command.split,
            )
        )
        at = await self.captured_at()
        result = EvaluationDatasetVersion(
            id=uuid4(),
            name=command.name,
            version=(latest or 0) + 1,
            split=command.split,
            dataset_hash=digest,
            cases=cases,
            frozen_by_membership_id=self.actor.membership_id,
            created_at=at,
        )
        self.session.add(
            DatasetRow(
                id=result.id,
                organization_id=self.org,
                name=result.name,
                version=result.version,
                split=result.split,
                policy_version=DATASET_POLICY,
                dataset_hash=digest,
                frozen_by_membership_id=result.frozen_by_membership_id,
                manifest=result.model_dump(mode="json"),
                created_at=at,
            )
        )
        await self.session.flush()
        for case in cases:
            self.session.add(
                MemberRow(
                    id=uuid4(),
                    organization_id=self.org,
                    dataset_id=result.id,
                    case_id=case.id,
                    case_version=case.version,
                    case_hash=case.case_hash,
                )
            )
        return result

    async def evidence(
        self, operation: str, key: str, identity: UUID | None, code: str | None = None
    ) -> None:
        self.session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id,
                action=f"evaluation.{operation}",
                outcome=AuditOutcome.REJECTED if code else AuditOutcome.SUCCEEDED,
                resource_type="evaluation",
                resource_id=identity,
                request_id=str(uuid4()),
                idempotency_key=key,
                before_data={},
                after_data={},
                reason_data={"reason_code": code} if code else {"policy_version": DATASET_POLICY},
            )
        )
        if code is None:
            self.session.add(
                OutboxEventModel(
                    id=uuid4(),
                    organization_id=self.org,
                    event_id=uuid4(),
                    event_type=f"evaluation.{operation}.v1",
                    aggregate_type="evaluation",
                    aggregate_id=identity,
                    payload={
                        "schema_version": "1.0",
                        "id": str(identity),
                        "policy_version": DATASET_POLICY,
                    },
                    status="PENDING",
                )
            )

    async def reject_curation(self, operation: str, key: str, code: str) -> None:
        await self.evidence(operation, key, None, code)
        await self.session.flush()
