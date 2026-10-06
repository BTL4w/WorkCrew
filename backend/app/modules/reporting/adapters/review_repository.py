"""One locked transaction for immutable edits, decisions, publication and quality feedback."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select, text

from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.audit.domain.events import AuditOutcome
from app.modules.feedback.adapters.repository import SQLFeedbackRepository
from app.modules.feedback.application.feedback_service import FeedbackService
from app.modules.feedback.domain.feedback import TerminalReviewCommand
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.planning_runs.adapters.database_models import OutboxEventModel
from app.modules.work.adapters.database_models import IdempotencyRecordModel, IdempotencyState
from work_management_ai.agents.reporting.contracts import FactBlock
from work_management_ai.agents.reporting.evaluators.numeric import verify_numeric

from ..domain.commands import (
    EditReportCommand,
    PublishReportCommand,
    RejectReportCommand,
    ReportEditVerificationCommand,
)
from ..domain.narrative import render_fact
from ..domain.reports import ReportError, ReportResult, ReportVersion, ReviewResult
from .database_models import (
    ReportModel,
    ReportPublicationModel,
    ReportReviewDecisionModel,
    ReportVersionModel,
)
from .generation_repository import enqueue_initial
from .narrative_runtime import to_reporting_snapshot
from .repository import SQLReportRepository
from .transaction import ReportTransactions
from .usage_models import ReportGenerationJobModel


class SQLReviewRepository(SQLFeedbackRepository):
    async def mutate(
        self,
        operation: str,
        report_id: UUID,
        command: EditReportCommand
        | PublishReportCommand
        | RejectReportCommand
        | ReportEditVerificationCommand,
        expected: int,
        key: str,
        fingerprint: str,
    ) -> ReportResult | ReviewResult:
        await self.authenticate()
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"{self.org}:{operation}:{self.actor.membership_id}:{key}"},
        )
        row = await self.session.scalar(
            select(ReportModel)
            .where(ReportModel.organization_id == self.org, ReportModel.id == report_id)
            .with_for_update()
        )
        if row is None:
            raise ReportError("RESOURCE_NOT_FOUND", 404)
        result = await self.get(report_id)
        replay = await self.session.scalar(
            select(IdempotencyRecordModel).where(
                IdempotencyRecordModel.organization_id == self.org,
                IdempotencyRecordModel.actor_membership_id == self.actor.membership_id,
                IdempotencyRecordModel.operation == operation,
                IdempotencyRecordModel.idempotency_key == key,
            )
        )
        if replay:
            if replay.request_fingerprint != fingerprint or replay.response_body is None:
                raise ReportError("IDEMPOTENCY_KEY_REUSED")
            if operation in ("report.edit", "report.edit.verify"):
                return await self.accessible_result(
                    ReportResult.model_validate(replay.response_body).model_copy(
                        update={"replayed": True}
                    )
                )
            value = ReviewResult.model_validate(replay.response_body)
            return value.model_copy(
                update={
                    "report_result": await self.accessible_result(
                        value.report_result.model_copy(update={"replayed": True})
                    )
                }
            )
        if row.version != expected:
            raise ReportError("STALE_REPORT_VERSION", 412)
        version = result.selected_version
        target = (
            command.edited_version_id
            if isinstance(command, ReportEditVerificationCommand)
            else (
                command.parent_version_id
                if isinstance(command, EditReportCommand)
                else command.report_version_id
            )
        )
        if target != version.id or command.snapshot_hash != result.snapshot.snapshot_hash:
            raise ReportError("REPORT_REVIEW_MISMATCH")
        job, original = await self.lineage(version)
        if await self.terminal(job.id) is not None:
            raise ReportError("REPORT_ALREADY_REVIEWED")
        at = await self.captured_at()
        outcome: ReportResult | ReviewResult
        if isinstance(command, ReportEditVerificationCommand):
            if (
                command.report_id != report_id
                or version.origin != "AI_EDITED"
                or command.parent_version_id != version.base_version_id
                or command.original_generation_id != job.id
            ):
                raise ReportError("REPORT_VERIFICATION_MISMATCH")
            active = await self.session.scalar(
                select(ReportGenerationJobModel.id).where(
                    ReportGenerationJobModel.organization_id == self.org,
                    ReportGenerationJobModel.report_id == report_id,
                    ReportGenerationJobModel.state.in_(("QUEUED", "RUNNING")),
                )
            )
            if active:
                raise ReportError("REPORT_GENERATION_PENDING")
            row.version += 1
            await self.session.flush()
            updated = await self.get(report_id)
            await enqueue_initial(
                self.session,
                actor=self.actor,
                result=updated,
                key=f"verify:{uuid4()}",
                job_type="EDIT_VERIFICATION",
                original_generation_id=job.id,
            )
            await self.session.flush()
            outcome = await self.get(report_id)
        elif isinstance(command, EditReportCommand):
            wire = to_reporting_snapshot(result.snapshot)
            if (
                command.narrative.locale != version.locale
                or not verify_numeric(wire, command.narrative, source_detail_limit=100).passed
            ):
                raise ReportError("REPORT_VERIFICATION_FAILED")
            previous = {b.id: b for b in version.narrative.blocks} if version.narrative else {}
            origins: dict[str, Literal["AI", "HUMAN"]] = {
                b.id: version.block_origins.get(b.id, "AI")
                if isinstance(b, FactBlock) or previous.get(b.id) == b
                else "HUMAN"
                for b in command.narrative.blocks
            }
            edited = ReportVersion(
                id=uuid4(),
                report_id=report_id,
                snapshot_id=result.snapshot.id,
                origin="AI_EDITED",
                locale=version.locale,
                created_at=at,
                narrative=command.narrative,
                block_origins=origins,
                rendered_facts={
                    b.id: render_fact(b, result.snapshot, version.locale)
                    for b in command.narrative.blocks
                    if isinstance(b, FactBlock)
                },
                provenance={
                    **original.provenance,
                    "original_version_id": str(original.id),
                    "parent_version_id": str(version.id),
                    "editor_membership_id": str(self.actor.membership_id),
                },
                generation_id=job.id,
                base_version_id=version.id,
            )
            self.session.add(
                ReportVersionModel(
                    id=edited.id,
                    organization_id=self.org,
                    report_id=report_id,
                    snapshot_id=edited.snapshot_id,
                    origin=edited.origin,
                    locale=edited.locale,
                    created_at=at,
                    payload=edited.model_dump(mode="json"),
                )
            )
            await self.session.flush()
            row.selected_version_id = edited.id
            row.version += 1
            await self.session.flush()
            updated = await self.get(report_id)
            verification_id = await enqueue_initial(
                self.session,
                actor=self.actor,
                result=updated,
                key=f"edit:{edited.id}",
                job_type="EDIT_VERIFICATION",
                original_generation_id=job.id,
            )
            await self.session.flush()
            outcome = (await self.get(report_id)).model_copy(
                update={
                    "generation_id": verification_id,
                    "generation_state": "QUEUED",
                    "verification_state": "PENDING",
                }
            )
        else:
            accepting = isinstance(command, PublishReportCommand)
            if accepting and result.narrative_access_state == "UNAVAILABLE":
                raise ReportError("REPORT_SOURCE_UNAVAILABLE")
            if accepting and (
                command.mode != "REVIEWED_NARRATIVE" or result.verification_state != "VERIFIED"
            ):
                raise ReportError("REPORT_VERIFICATION_REQUIRED")
            # Validate bounded sources/bindings again under current authorization.
            if accepting and (
                version.narrative is None
                or not verify_numeric(
                    to_reporting_snapshot(result.snapshot),
                    version.narrative,
                    source_detail_limit=100,
                ).passed
            ):
                raise ReportError("REPORT_VERIFICATION_FAILED")
            decision_id = uuid4()
            self.session.add(
                ReportReviewDecisionModel(
                    id=decision_id,
                    organization_id=self.org,
                    report_id=report_id,
                    report_version_id=version.id,
                    snapshot_hash=command.snapshot_hash,
                    actor_membership_id=self.actor.membership_id,
                    expected_report_version=expected,
                    kind="ACCEPT" if accepting else "REJECT",
                    decided_at=at,
                )
            )
            await self.session.flush()
            if accepting:
                publication_id = uuid4()
                self.session.add(
                    ReportPublicationModel(
                        id=publication_id,
                        organization_id=self.org,
                        report_id=report_id,
                        report_version_id=version.id,
                        snapshot_hash=command.snapshot_hash,
                        publisher_membership_id=self.actor.membership_id,
                        decision_id=decision_id,
                        published_at=at,
                    )
                )
                row.current_publication_id = publication_id
                self.session.add(
                    OutboxEventModel(
                        id=uuid4(),
                        organization_id=self.org,
                        event_id=uuid4(),
                        event_type="report.published.v1",
                        aggregate_type="report",
                        aggregate_id=report_id,
                        payload={
                            "schema_version": "1.0",
                            "report_id": str(report_id),
                            "publication_id": str(publication_id),
                            "report_version_id": str(version.id),
                            "snapshot_hash": command.snapshot_hash,
                            "actor_membership_id": str(self.actor.membership_id),
                        },
                        status="PENDING",
                    )
                )
            row.version += 1
            await self.session.flush()
            feedback = await FeedbackService.record_terminal(
                self,
                TerminalReviewCommand(
                    report_id=report_id,
                    report_version_id=version.id,
                    original_version_id=original.id,
                    generation_id=job.id,
                    decision_id=decision_id,
                    provenance=original.provenance,
                    decision="EDIT"
                    if accepting and version.origin == "AI_EDITED"
                    else "ACCEPT"
                    if accepting
                    else "REJECT",
                    reason="Reviewed exact verified version" if accepting else command.reason,
                ),
            )
            outcome = ReviewResult(
                report_result=await self.get(report_id),
                decision_id=decision_id,
                terminal_outcome_id=feedback.feedback.id,
            )
        self.session.add(
            IdempotencyRecordModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id,
                operation=operation,
                idempotency_key=key,
                request_fingerprint=fingerprint,
                state=IdempotencyState.COMPLETED,
                response_status=201,
                response_body=outcome.model_dump(mode="json"),
                expires_at=at + timedelta(days=7),
            )
        )
        self.session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id,
                action=operation,
                outcome=AuditOutcome.SUCCEEDED,
                resource_type="report",
                resource_id=report_id,
                request_id=str(uuid4()),
                idempotency_key=key,
                before_data={"version": expected, "selected_version_id": str(version.id)},
                after_data={
                    "version": row.version,
                    "selected_version_id": str(row.selected_version_id),
                },
                reason_data={},
            )
        )
        await self.session.flush()
        return outcome

    async def audit_review_rejection(
        self, operation: str, report_id: UUID | None, key: str | None, code: str
    ) -> None:
        self.session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id,
                action=operation,
                outcome=AuditOutcome.REJECTED,
                resource_type="report",
                resource_id=report_id,
                request_id=str(uuid4()),
                idempotency_key=key,
                before_data={},
                after_data={},
                reason_data={"reason_code": code},
            )
        )
        await self.session.flush()


class ReviewTransactions(ReportTransactions):
    @asynccontextmanager
    async def __call__(self, actor: AuthenticatedActor) -> AsyncGenerator[SQLReviewRepository]:
        async with super().__call__(actor) as base:
            assert isinstance(base, SQLReportRepository)
            yield SQLReviewRepository(base.session, actor, self.default_timezone)
