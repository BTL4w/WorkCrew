"""Feedback shares the caller's transaction; lineage never comes from the client."""

from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, text

from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.audit.domain.events import AuditOutcome
from app.modules.planning_runs.adapters.database_models import OutboxEventModel
from app.modules.reporting.adapters.database_models import ReportVersionModel
from app.modules.reporting.adapters.repository import SQLReportRepository, version_domain
from app.modules.reporting.adapters.usage_models import ReportGenerationJobModel
from app.modules.reporting.domain.reports import ReportError, ReportVersion
from app.modules.work.adapters.database_models import IdempotencyRecordModel, IdempotencyState

from ..domain.feedback import Feedback, FeedbackCommand, FeedbackResult, TerminalReviewCommand
from ..domain.outcomes import FeedbackOutcome, OutcomeSourceCommand, ReviewRates
from .database_models import FeedbackModel


class SQLFeedbackRepository(SQLReportRepository):
    async def lineage(
        self, version: ReportVersion
    ) -> tuple[ReportGenerationJobModel, ReportVersion]:
        if version.origin not in ("AI_PROPOSED", "AI_EDITED") or version.generation_id is None:
            raise ReportError("REPORT_AI_LINEAGE_REQUIRED")
        job = await self.session.scalar(
            select(ReportGenerationJobModel).where(
                ReportGenerationJobModel.organization_id == self.org,
                ReportGenerationJobModel.id == version.generation_id,
                ReportGenerationJobModel.report_id == version.report_id,
                ReportGenerationJobModel.job_type == "DRAFT",
            )
        )
        original = await self.session.scalar(
            select(ReportVersionModel).where(
                ReportVersionModel.organization_id == self.org,
                ReportVersionModel.report_id == version.report_id,
                ReportVersionModel.id == (job.proposed_version_id if job else None),
            )
        )
        if job is None or original is None or original.origin != "AI_PROPOSED":
            raise ReportError("REPORT_AI_LINEAGE_REQUIRED")
        return job, version_domain(original)

    async def terminal(self, generation_id: UUID) -> FeedbackModel | None:
        return await self.session.scalar(
            select(FeedbackModel).where(
                FeedbackModel.organization_id == self.org,
                FeedbackModel.generation_id == generation_id,
                FeedbackModel.kind == "TERMINAL_QUALITY",
            )
        )

    async def append(
        self, command: FeedbackCommand, version: ReportVersion, *, decision_id: UUID | None = None
    ) -> FeedbackResult:
        job, original = await self.lineage(version)
        if decision_id is not None and await self.terminal(job.id) is not None:
            raise ReportError("REPORT_ALREADY_REVIEWED")
        at = await self.captured_at()
        value = Feedback(
            id=uuid4(),
            report_id=command.report_id,
            report_version_id=version.id,
            original_version_id=original.id,
            generation_id=job.id,
            actor_membership_id=self.actor.membership_id,
            decision_id=decision_id,
            kind="TERMINAL_QUALITY" if decision_id else "ADVISORY",
            decision=command.decision,
            reason=command.reason,
            provenance=original.provenance,
            created_at=at,
        )
        self.session.add(
            FeedbackModel(
                organization_id=self.org,
                snapshot_hash=(await self.get(command.report_id)).snapshot.snapshot_hash,
                **value.model_dump(mode="python"),
            )
        )
        self.session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id,
                action="feedback.recorded",
                outcome=AuditOutcome.SUCCEEDED,
                resource_type="feedback",
                resource_id=value.id,
                request_id=str(uuid4()),
                before_data={},
                after_data={
                    "report_version_id": str(version.id),
                    "generation_id": str(job.id),
                    "kind": value.kind,
                    "decision": value.decision,
                },
                reason_data={},
            )
        )
        self.session.add(
            OutboxEventModel(
                id=uuid4(),
                organization_id=self.org,
                event_id=uuid4(),
                event_type="feedback.recorded.v1",
                aggregate_type="feedback",
                aggregate_id=value.id,
                payload={
                    "schema_version": "1.0",
                    "feedback_id": str(value.id),
                    "report_id": str(version.report_id),
                },
                status="PENDING",
            )
        )
        await self.session.flush()
        return FeedbackResult(feedback=value)

    async def record_terminal(self, command: TerminalReviewCommand) -> FeedbackResult:
        row = await self.session.scalar(
            select(ReportVersionModel).where(
                ReportVersionModel.organization_id == self.org,
                ReportVersionModel.report_id == command.report_id,
                ReportVersionModel.id == command.report_version_id,
            )
        )
        if row is None:
            raise ReportError("RESOURCE_NOT_FOUND", 404)
        version = version_domain(row)
        job, original = await self.lineage(version)
        if (
            command.generation_id != job.id
            or command.original_version_id != original.id
            or command.provenance != original.provenance
        ):
            raise ReportError("REPORT_AI_LINEAGE_REQUIRED")
        return await self.append(command, version, decision_id=command.decision_id)

    async def record(self, command: FeedbackCommand, key: str, fingerprint: str) -> FeedbackResult:
        await self.authenticate()
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"{self.org}:feedback:{self.actor.membership_id}:{key}"},
        )
        await self.get(command.report_id)
        version_row = await self.session.scalar(
            select(ReportVersionModel).where(
                ReportVersionModel.organization_id == self.org,
                ReportVersionModel.report_id == command.report_id,
                ReportVersionModel.id == command.report_version_id,
            )
        )
        if version_row is None:
            raise ReportError("RESOURCE_NOT_FOUND", 404)
        replay = await self.session.scalar(
            select(IdempotencyRecordModel).where(
                IdempotencyRecordModel.organization_id == self.org,
                IdempotencyRecordModel.actor_membership_id == self.actor.membership_id,
                IdempotencyRecordModel.operation == "feedback.record",
                IdempotencyRecordModel.idempotency_key == key,
            )
        )
        if replay:
            if replay.request_fingerprint != fingerprint or replay.response_body is None:
                raise ReportError("IDEMPOTENCY_KEY_REUSED")
            return FeedbackResult.model_validate(replay.response_body).model_copy(
                update={"replayed": True}
            )
        result = await self.append(command, version_domain(version_row))
        self.session.add(
            IdempotencyRecordModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id,
                operation="feedback.record",
                idempotency_key=key,
                request_fingerprint=fingerprint,
                state=IdempotencyState.COMPLETED,
                response_status=201,
                response_body=result.model_dump(mode="json"),
                expires_at=result.feedback.created_at + timedelta(days=7),
            )
        )
        return result

    async def rejected(self, command: FeedbackCommand | None, key: str | None, code: str) -> None:
        self.session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id,
                action="feedback.recorded",
                outcome=AuditOutcome.REJECTED,
                resource_type="report",
                resource_id=command.report_id if command else None,
                request_id=str(uuid4()),
                idempotency_key=key,
                before_data={},
                after_data={},
                reason_data={"reason_code": code},
            )
        )
        await self.session.flush()

    async def rates(self, project_id: UUID) -> ReviewRates:
        from .projection import project_rates

        await self.authenticate()
        await self.authorize_project(project_id)
        return await project_rates(self.session, self.org, project_id)

    async def record_outcome(
        self, feedback_id: UUID, source: OutcomeSourceCommand, key: str, fingerprint: str
    ) -> FeedbackOutcome:
        from .database_models import FeedbackOutcomeModel
        from .outcome_reader import SQLOutcomeReader
        from .projection import outcome_domain

        await self.authenticate()
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"{self.org}:feedback.outcome:{self.actor.membership_id}:{key}"},
        )
        # Different keys for the same source serialize before source-version deduplication.
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {
                "key": (
                    f"{self.org}:outcome:{feedback_id}:{source.source_type}:"
                    f"{source.source_id}:{source.source_version}"
                )
            },
        )
        feedback = await self.session.scalar(
            select(FeedbackModel).where(
                FeedbackModel.organization_id == self.org, FeedbackModel.id == feedback_id
            )
        )
        if feedback is None:
            raise ReportError("RESOURCE_NOT_FOUND", 404)
        report = await self.get(feedback.report_id)
        replay = await self.session.scalar(
            select(IdempotencyRecordModel).where(
                IdempotencyRecordModel.organization_id == self.org,
                IdempotencyRecordModel.actor_membership_id == self.actor.membership_id,
                IdempotencyRecordModel.operation == "feedback.outcome.record",
                IdempotencyRecordModel.idempotency_key == key,
            )
        )
        if replay and replay.request_fingerprint != fingerprint:
            raise ReportError("IDEMPOTENCY_KEY_REUSED")
        existing = await self.session.scalar(
            select(FeedbackOutcomeModel).where(
                FeedbackOutcomeModel.organization_id == self.org,
                FeedbackOutcomeModel.feedback_id == feedback_id,
                FeedbackOutcomeModel.source_type == source.source_type,
                FeedbackOutcomeModel.source_id == source.source_id,
                FeedbackOutcomeModel.source_version == source.source_version,
            )
        )
        if existing:
            # Current source access is required for every replay. Frozen actuals remain readable
            # after a later observation; do not require that a historical version stays current.
            from .projection import outcome_accessible

            if not await outcome_accessible(
                self.session, self.org, report.report.project_id, existing
            ):
                raise ReportError("RESOURCE_NOT_FOUND", 404)
            result = outcome_domain(existing)
        else:
            evidence = await SQLOutcomeReader(self.session).read_outcome(self.actor, source)
            if evidence.project_id != report.report.project_id:
                raise ReportError("OUTCOME_SOURCE_PROJECT_MISMATCH", 404)
            at = await self.captured_at()
            existing = FeedbackOutcomeModel(
                id=uuid4(),
                organization_id=self.org,
                feedback_id=feedback_id,
                actor_membership_id=self.actor.membership_id,
                schema_version="feedback-outcome.v1",
                source_type=source.source_type,
                source_id=source.source_id,
                source_version=source.source_version,
                task_transition_id=source.source_id
                if source.source_type == "TASK_TRANSITION"
                else None,
                actual_task_id=source.source_id if source.source_type == "TASK_ACTUALS" else None,
                blocker_transition_id=source.source_id
                if source.source_type == "BLOCKER_RESOLUTION"
                else None,
                observation_id=evidence.observation_id,
                state=evidence.state,
                facts=evidence.facts,
                occurred_at=evidence.occurred_at,
                recorded_at=at,
            )
            self.session.add(existing)
            await self.session.flush()
            result = outcome_domain(existing)
            self.session.add(
                AuditEventModel(
                    id=uuid4(),
                    organization_id=self.org,
                    actor_membership_id=self.actor.membership_id,
                    action="feedback.outcome.recorded",
                    outcome=AuditOutcome.SUCCEEDED,
                    resource_type="feedback",
                    resource_id=feedback_id,
                    request_id=str(uuid4()),
                    idempotency_key=key,
                    before_data={},
                    after_data={
                        "outcome_id": str(result.id),
                        "source": source.model_dump(mode="json"),
                        "state": result.state,
                    },
                    reason_data={},
                )
            )
            self.session.add(
                OutboxEventModel(
                    id=uuid4(),
                    organization_id=self.org,
                    event_id=uuid4(),
                    event_type="feedback.outcome.recorded.v1",
                    aggregate_type="feedback",
                    aggregate_id=feedback_id,
                    payload={
                        "schema_version": "1.0",
                        "feedback_id": str(feedback_id),
                        "report_id": str(report.report.id),
                        "outcome_id": str(result.id),
                    },
                    status="PENDING",
                )
            )
        if replay is None:
            self.session.add(
                IdempotencyRecordModel(
                    id=uuid4(),
                    organization_id=self.org,
                    actor_membership_id=self.actor.membership_id,
                    operation="feedback.outcome.record",
                    idempotency_key=key,
                    request_fingerprint=fingerprint,
                    state=IdempotencyState.COMPLETED,
                    response_status=201,
                    response_body=result.model_dump(mode="json"),
                    expires_at=(await self.captured_at()) + timedelta(days=7),
                )
            )
        return result

    async def reject_outcome(self, feedback_id: UUID | None, key: str | None, code: str) -> None:
        self.session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id,
                action="feedback.outcome.recorded",
                outcome=AuditOutcome.REJECTED,
                resource_type="feedback",
                resource_id=feedback_id,
                request_id=str(uuid4()),
                idempotency_key=key,
                before_data={},
                after_data={},
                reason_data={"reason_code": code},
            )
        )
        await self.session.flush()
