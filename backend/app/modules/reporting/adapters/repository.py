"""Reports, source inventories, audit and replay share one authorized SQL transaction."""

import base64
import json
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.audit.domain.events import AuditOutcome
from app.modules.automations.adapters.database_models import ScheduleModel, ScheduleVersionModel
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.planning_runs.adapters.database_models import OutboxEventModel
from app.modules.work.adapters.database_models import (
    IdempotencyRecordModel,
    IdempotencyState,
    ProjectModel,
)

from ..application.ports import ReportSnapshotReadPort
from ..domain.commands import CreateReportCommand, PublishReportCommand
from ..domain.events import MetricsCaptured, ReportPublished
from ..domain.generation import GenerationState
from ..domain.reports import (
    Report,
    ReportError,
    ReportPage,
    ReportPublication,
    ReportResult,
    ReportSourceItem,
    ReportSourcePage,
    ReportVersion,
)
from ..domain.snapshots import ReportMetricSnapshot
from .database_models import (
    ReportModel,
    ReportPublicationModel,
    ReportReceiptModel,
    ReportReviewDecisionModel,
    ReportSnapshotModel,
    ReportSourceModel,
    ReportVersionModel,
)
from .snapshot_reader import SQLReportSnapshotReader
from .source_reader import source_freshness


def report_domain(row: ReportModel) -> Report:
    return Report.model_validate({key: getattr(row, key) for key in Report.model_fields})


def version_domain(row: ReportVersionModel) -> ReportVersion:
    return ReportVersion.model_validate(
        {
            **row.payload,
            **{
                key: getattr(row, key)
                for key in ("id", "report_id", "snapshot_id", "origin", "locale", "created_at")
            },
        }
    )


class SQLReportRepository:
    def __init__(self, session: AsyncSession, actor: AuthenticatedActor, default_timezone: str):
        self.session, self.actor, self.org = session, actor, actor.organization_id
        self.default_timezone = default_timezone
        self.snapshot_reader: ReportSnapshotReadPort = SQLReportSnapshotReader(
            session, actor, default_timezone
        )

    async def authenticate(self) -> None:
        active = await self.session.scalar(
            text(
                "SELECT public.lock_active_membership(:org,:member) "
                "AND EXISTS(SELECT 1 FROM memberships WHERE organization_id=:org AND id=:member "
                "AND user_id=:user AND role IN ('MANAGER','ADMIN'))"
            ),
            {"org": self.org, "member": self.actor.membership_id, "user": self.actor.user_id},
        )
        if active is not True:
            raise ReportError("FORBIDDEN", 403)

    async def authorize_project(self, project_id: UUID) -> None:
        found = await self.session.scalar(
            select(ProjectModel.id).where(
                ProjectModel.organization_id == self.org, ProjectModel.id == project_id
            )
        )
        if found is None:
            raise ReportError("RESOURCE_NOT_FOUND", 404)

    async def captured_at(self) -> datetime:
        result = await self.session.scalar(select(func.transaction_timestamp()))
        if not isinstance(result, datetime):
            raise ReportError("REPORT_CAPTURE_FAILED", 409)
        return result

    async def timezone(self, project_id: UUID) -> str:
        row = await self.session.scalar(
            select(ScheduleModel).where(
                ScheduleModel.organization_id == self.org, ScheduleModel.project_id == project_id
            )
        )
        if row is not None:
            config = await self.session.scalar(
                select(ScheduleVersionModel).where(
                    ScheduleVersionModel.organization_id == self.org,
                    ScheduleVersionModel.schedule_id == row.id,
                    ScheduleVersionModel.version == row.version,
                )
            )
            if config is not None:
                value = config.payload.get("timezone")
                if isinstance(value, str):
                    return value
        return self.default_timezone

    async def replay(self, key: str, fingerprint: str) -> UUID | None:
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"{self.org}:report.create:{self.actor.membership_id}:{key}"},
        )
        row = await self.session.scalar(
            select(IdempotencyRecordModel).where(
                IdempotencyRecordModel.organization_id == self.org,
                IdempotencyRecordModel.actor_membership_id == self.actor.membership_id,
                IdempotencyRecordModel.operation == "report.create",
                IdempotencyRecordModel.idempotency_key == key,
            )
        )
        if row is None:
            return None
        if row.request_fingerprint != fingerprint or row.response_body is None:
            raise ReportError("IDEMPOTENCY_KEY_REUSED", 409)
        return UUID(str(row.response_body["report_id"]))

    async def save(
        self,
        snapshot: ReportMetricSnapshot,
        command: CreateReportCommand,
        version_id: UUID,
        key: str,
        fingerprint: str,
        request_id: str,
    ) -> ReportResult:
        report = Report(
            id=snapshot.report_id,
            organization_id=self.org,
            project_id=command.project_id,
            kind=command.kind,
            locale=command.locale,
            version=1,
            snapshot_id=snapshot.id,
            selected_version_id=version_id,
            created_by_membership_id=self.actor.membership_id,
            narrative_requested=command.narrative_enabled,
            created_at=snapshot.captured_at,
        )
        version = ReportVersion(
            id=version_id,
            report_id=report.id,
            snapshot_id=snapshot.id,
            locale=command.locale,
            created_at=snapshot.captured_at,
        )
        self.session.add(ReportModel(**report.model_dump(mode="python")))
        self.session.add(
            ReportSnapshotModel(
                id=snapshot.id,
                organization_id=self.org,
                report_id=report.id,
                project_id=command.project_id,
                snapshot_hash=snapshot.snapshot_hash,
                payload=snapshot.model_dump(mode="json"),
                captured_at=snapshot.captured_at,
            )
        )
        self.session.add(
            ReportVersionModel(
                organization_id=self.org,
                **version.model_dump(
                    mode="python",
                    exclude={
                        "block_origins",
                        "narrative_access_state",
                        "narrative",
                        "rendered_facts",
                        "provenance",
                        "generation_id",
                        "base_version_id",
                    },
                ),
                payload={},
            )
        )
        # Parent facts must be flushed before indexed sources/receipts (nondeferrable FKs).
        await self.session.flush()
        for source in snapshot.sources:
            if source.resource_type != "TASK":
                continue
            self.session.add(
                ReportSourceModel(
                    id=uuid4(),
                    organization_id=self.org,
                    snapshot_id=snapshot.id,
                    task_id=source.resource_id,
                    payload=source.model_dump(mode="json"),
                )
            )
        for receipt in snapshot.receipts:
            self.session.add(
                ReportReceiptModel(
                    id=receipt.id,
                    organization_id=self.org,
                    snapshot_id=snapshot.id,
                    payload=receipt.model_dump(mode="json"),
                )
            )
        self.session.add(
            IdempotencyRecordModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id,
                operation="report.create",
                idempotency_key=key,
                request_fingerprint=fingerprint,
                state=IdempotencyState.COMPLETED,
                response_status=201,
                response_body={"report_id": str(report.id)},
                expires_at=snapshot.captured_at + timedelta(days=7),
            )
        )
        self.session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id,
                action="report.created",
                outcome=AuditOutcome.SUCCEEDED,
                resource_type="report",
                resource_id=report.id,
                request_id=request_id,
                idempotency_key=key,
                before_data={},
                after_data={
                    "snapshot_id": str(snapshot.id),
                    "snapshot_hash": snapshot.snapshot_hash,
                },
                reason_data={},
            )
        )
        self.session.add(
            OutboxEventModel(
                id=uuid4(),
                organization_id=self.org,
                event_id=uuid4(),
                event_type="report.metrics_captured.v1",
                aggregate_type="report",
                aggregate_id=report.id,
                payload=MetricsCaptured(
                    report_id=report.id,
                    snapshot_id=snapshot.id,
                    snapshot_hash=snapshot.snapshot_hash,
                    actor_membership_id=self.actor.membership_id,
                ).model_dump(mode="json"),
                status="PENDING",
            )
        )
        await self.session.flush()
        result = ReportResult(
            report=report,
            snapshot=snapshot,
            selected_version=version,
            generation_state="NOT_REQUESTED",
            metrics_version_id=version.id,
        )
        if command.narrative_enabled:
            from .generation_repository import enqueue_initial

            generation_id = await enqueue_initial(
                self.session, actor=self.actor, result=result, key=str(report.id)
            )
            result = result.model_copy(
                update={"generation_state": "QUEUED", "generation_id": generation_id}
            )
        return result

    async def narrative_available(
        self, version: ReportVersion, snapshot: ReportMetricSnapshot
    ) -> bool:
        from ..domain.narrative import FactBlock
        from . import source_reader

        if version.narrative is None:
            return version.narrative_access_state == "AVAILABLE"
        refs = {
            (ref.resource_type, ref.resource_id, ref.version, ref.fingerprint)
            for block in version.narrative.blocks
            for ref in (
                block.source_bindings if isinstance(block, FactBlock) else block.source_refs
            )
        }
        captured = {
            (ref.resource_type, ref.resource_id, ref.version, ref.fingerprint): ref
            for ref in snapshot.sources
        }
        for identity in refs:
            ref = captured.get(identity)
            if (
                ref is None
                or await source_reader.source_freshness(self.session, self.actor, ref)
                == "UNAVAILABLE"
            ):
                return False
        return True

    async def accessible_version(
        self, version: ReportVersion, snapshot: ReportMetricSnapshot
    ) -> ReportVersion:
        if await self.narrative_available(version, snapshot):
            return version
        return version.model_copy(
            update={
                "narrative": None,
                "rendered_facts": {},
                "narrative_access_state": "UNAVAILABLE",
            }
        )

    async def accessible_result(self, result: ReportResult) -> ReportResult:
        selected = await self.accessible_version(result.selected_version, result.snapshot)
        published = tuple(
            [
                await self.accessible_version(version, result.snapshot)
                for version in result.published_versions
            ]
        )
        return result.model_copy(
            update={
                "selected_version": selected,
                "published_versions": published,
                "narrative_access_state": selected.narrative_access_state,
            }
        )

    async def get(self, report_id: UUID, *, replayed: bool = False) -> ReportResult:
        row = await self.session.scalar(
            select(ReportModel).where(
                ReportModel.organization_id == self.org, ReportModel.id == report_id
            )
        )
        if row is None:
            raise ReportError("RESOURCE_NOT_FOUND", 404)
        await self.authorize_project(row.project_id)
        snapshot_row = await self.session.scalar(
            select(ReportSnapshotModel).where(
                ReportSnapshotModel.organization_id == self.org,
                ReportSnapshotModel.id == row.snapshot_id,
                ReportSnapshotModel.report_id == row.id,
            )
        )
        version_row = await self.session.scalar(
            select(ReportVersionModel).where(
                ReportVersionModel.organization_id == self.org,
                ReportVersionModel.id == row.selected_version_id,
                ReportVersionModel.report_id == row.id,
            )
        )
        if snapshot_row is None or version_row is None:
            raise ReportError("REPORT_CAPTURE_FAILED", 409)
        snapshot = ReportMetricSnapshot.model_validate(snapshot_row.payload)
        if not snapshot.verified_hash() or snapshot.snapshot_hash != snapshot_row.snapshot_hash:
            raise ReportError("REPORT_CAPTURE_FAILED", 409)
        publication_rows = await self.session.scalars(
            select(ReportPublicationModel)
            .where(
                ReportPublicationModel.organization_id == self.org,
                ReportPublicationModel.report_id == row.id,
            )
            .order_by(ReportPublicationModel.published_at.desc(), ReportPublicationModel.id)
        )
        publications = tuple(
            ReportPublication.model_validate(
                {key: getattr(publication, key) for key in ReportPublication.model_fields}
            )
            for publication in publication_rows
        )
        from .usage_models import ReportGenerationJobModel

        generation = await self.session.scalar(
            select(ReportGenerationJobModel)
            .where(
                ReportGenerationJobModel.organization_id == self.org,
                ReportGenerationJobModel.report_id == row.id,
            )
            .order_by(
                ReportGenerationJobModel.created_at.desc(), ReportGenerationJobModel.id.desc()
            )
            .limit(1)
        )
        metrics_version = await self.session.scalar(
            select(ReportVersionModel.id)
            .where(
                ReportVersionModel.organization_id == self.org,
                ReportVersionModel.report_id == row.id,
                ReportVersionModel.origin == "METRICS_ONLY",
            )
            .order_by(ReportVersionModel.created_at)
            .limit(1)
        )
        from app.modules.feedback.adapters.database_models import (
            FeedbackModel,
            ReportVerificationModel,
        )

        verification_state = "NOT_APPLICABLE"
        review_state = "PENDING"
        version = version_domain(version_row)
        if version.origin == "AI_PROPOSED":
            verification_state = "VERIFIED"
        elif version.origin == "AI_EDITED":
            verified = await self.session.scalar(
                select(ReportVerificationModel.id).where(
                    ReportVerificationModel.organization_id == self.org,
                    ReportVerificationModel.report_version_id == version.id,
                )
            )
            verification_state = (
                "VERIFIED"
                if verified
                else "FAILED"
                if generation and generation.state in ("FAILED", "AI_UNAVAILABLE")
                else "PENDING"
            )
        if version.generation_id:
            terminal = await self.session.scalar(
                select(FeedbackModel).where(
                    FeedbackModel.organization_id == self.org,
                    FeedbackModel.generation_id == version.generation_id,
                    FeedbackModel.kind == "TERMINAL_QUALITY",
                )
            )
            if terminal:
                review_state = "REJECTED" if terminal.decision == "REJECT" else "ACCEPTED"
        published_rows = await self.session.scalars(
            select(ReportVersionModel).where(
                ReportVersionModel.organization_id == self.org,
                ReportVersionModel.report_id == row.id,
                ReportVersionModel.id.in_([p.report_version_id for p in publications]),
            )
        )
        published_versions = tuple(version_domain(v) for v in published_rows)
        return await self.accessible_result(
            ReportResult(
                publications=publications,
                verification_state=verification_state,
                review_state=review_state,
                published_versions=published_versions,
                generation_id=generation.id if generation else None,
                metrics_version_id=metrics_version,
                report=report_domain(row),
                snapshot=snapshot,
                selected_version=version_domain(version_row),
                generation_state=cast(
                    "GenerationState",
                    generation.state
                    if generation
                    else ("AI_UNAVAILABLE" if row.narrative_requested else "NOT_REQUESTED"),
                ),
                replayed=replayed,
            )
        )

    async def list(self, project_id: UUID, page: int, page_size: int) -> ReportPage:
        await self.authorize_project(project_id)
        predicate = (ReportModel.organization_id == self.org, ReportModel.project_id == project_id)
        count = await self.session.scalar(
            select(func.count()).select_from(ReportModel).where(*predicate)
        )
        rows = await self.session.scalars(
            select(ReportModel)
            .where(*predicate)
            .order_by(ReportModel.created_at.desc(), ReportModel.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return ReportPage(
            items=tuple(report_domain(row) for row in rows),
            page=page,
            page_size=page_size,
            total=count or 0,
        )

    async def sources(
        self, report_id: UUID, cursor: str | None, page_size: int
    ) -> ReportSourcePage:
        result = await self.get(report_id)
        snapshot = result.snapshot
        offset = 0
        if cursor:
            try:
                decoded: object = json.loads(base64.urlsafe_b64decode(cursor).decode())
                if not isinstance(decoded, list):
                    raise ValueError("invalid cursor shape")
                binding = cast(list[object], decoded)
                if (
                    len(binding) != 2
                    or binding[0] != snapshot.snapshot_hash
                    or type(binding[1]) is not int
                    or not 0 <= binding[1] <= len(snapshot.sources)
                ):
                    raise ValueError("invalid cursor binding")
                offset = binding[1]
            except (ValueError, UnicodeError, TypeError) as exc:
                raise ReportError("VALIDATION_FAILED", 422) from exc
        refs = snapshot.sources[offset : offset + page_size]
        items = tuple(
            [
                ReportSourceItem(
                    source=source,
                    freshness=await source_freshness(self.session, self.actor, source),
                )
                for source in refs
            ]
        )
        next_offset = offset + len(items)
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps([snapshot.snapshot_hash, next_offset], separators=(",", ":")).encode()
            ).decode()
            if next_offset < len(snapshot.sources)
            else None
        )
        return ReportSourcePage(
            snapshot_hash=snapshot.snapshot_hash,
            items=items,
            receipts=snapshot.receipts,
            next_cursor=next_cursor,
            total=len(snapshot.sources),
        )

    async def audit_rejection(
        self, request_id: str, key: str | None, code: str, project_id: UUID | None
    ) -> None:
        self.session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id,
                action="report.created",
                outcome=AuditOutcome.REJECTED,
                resource_type="project",
                resource_id=project_id,
                request_id=request_id,
                idempotency_key=key,
                before_data={},
                after_data={},
                reason_data={"reason_code": code},
            )
        )
        await self.session.flush()

    async def publish(
        self,
        report_id: UUID,
        command: PublishReportCommand,
        expected_version: int,
        key: str,
        fingerprint: str,
        request_id: str,
    ) -> ReportResult:
        if command.mode != "METRICS_ONLY":
            raise ReportError("REPORT_VERIFICATION_REQUIRED")
        # Lock before reading the resource; retry a stale RR snapshot on serialization failure.
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"{self.org}:report.publish:{self.actor.membership_id}:{key}"},
        )
        row = await self.session.scalar(
            select(ReportModel)
            .where(
                ReportModel.organization_id == self.org,
                ReportModel.id == report_id,
            )
            .with_for_update()
        )
        if row is None:
            raise ReportError("RESOURCE_NOT_FOUND", 404)
        await self.authorize_project(row.project_id)
        replay = await self.session.scalar(
            select(IdempotencyRecordModel).where(
                IdempotencyRecordModel.organization_id == self.org,
                IdempotencyRecordModel.actor_membership_id == self.actor.membership_id,
                IdempotencyRecordModel.operation == "report.publish",
                IdempotencyRecordModel.idempotency_key == key,
            )
        )
        if replay is not None:
            if replay.request_fingerprint != fingerprint or replay.response_body is None:
                raise ReportError("IDEMPOTENCY_KEY_REUSED", 409)
            return await self.accessible_result(
                ReportResult.model_validate(replay.response_body).model_copy(
                    update={"replayed": True}
                )
            )
        if row.version != expected_version:
            raise ReportError("STALE_REPORT_VERSION", 412)
        result = await self.get(report_id)
        metrics_version = await self.session.scalar(
            select(ReportVersionModel).where(
                ReportVersionModel.organization_id == self.org,
                ReportVersionModel.report_id == row.id,
                ReportVersionModel.id == command.report_version_id,
            )
        )
        if (
            metrics_version is None
            or metrics_version.origin != "METRICS_ONLY"
            or command.snapshot_hash != result.snapshot.snapshot_hash
            or metrics_version.snapshot_id != result.snapshot.id
        ):
            raise ReportError("REPORT_PUBLICATION_MISMATCH", 409)
        # Current Manager scope authorizes all project source facts. Source changes are
        # freshness information, never a reason to rewrite the verified historical snapshot.
        at = await self.captured_at()
        decision_id = uuid4()
        self.session.add(
            ReportReviewDecisionModel(
                id=decision_id,
                organization_id=self.org,
                report_id=report_id,
                report_version_id=command.report_version_id,
                snapshot_hash=command.snapshot_hash,
                actor_membership_id=self.actor.membership_id,
                expected_report_version=expected_version,
                kind="METRICS_ONLY_PUBLISHED",
                decided_at=at,
            )
        )
        await self.session.flush()
        publication = ReportPublication(
            id=uuid4(),
            report_id=report_id,
            report_version_id=command.report_version_id,
            snapshot_hash=command.snapshot_hash,
            publisher_membership_id=self.actor.membership_id,
            decision_id=decision_id,
            published_at=at,
        )
        self.session.add(
            ReportPublicationModel(
                organization_id=self.org, **publication.model_dump(mode="python")
            )
        )
        row.current_publication_id = publication.id
        row.version += 1
        await self.session.flush()
        published = await self.get(report_id)
        self.session.add(
            IdempotencyRecordModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id,
                operation="report.publish",
                idempotency_key=key,
                request_fingerprint=fingerprint,
                state=IdempotencyState.COMPLETED,
                response_status=201,
                response_body=published.model_dump(mode="json"),
                expires_at=at + timedelta(days=7),
            )
        )
        self.session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id,
                action="report.published",
                outcome=AuditOutcome.SUCCEEDED,
                resource_type="report",
                resource_id=report_id,
                request_id=request_id,
                idempotency_key=key,
                before_data={
                    "version": expected_version,
                    "current_publication_id": str(result.report.current_publication_id)
                    if result.report.current_publication_id
                    else None,
                },
                after_data={
                    "version": row.version,
                    "publication_id": str(publication.id),
                    "report_version_id": str(command.report_version_id),
                    "snapshot_hash": command.snapshot_hash,
                },
                reason_data={},
            )
        )
        self.session.add(
            OutboxEventModel(
                id=uuid4(),
                organization_id=self.org,
                event_id=uuid4(),
                event_type="report.published.v1",
                aggregate_type="report",
                aggregate_id=report_id,
                payload=ReportPublished(
                    report_id=report_id,
                    publication_id=publication.id,
                    report_version_id=command.report_version_id,
                    snapshot_hash=command.snapshot_hash,
                    actor_membership_id=self.actor.membership_id,
                ).model_dump(mode="json"),
                status="PENDING",
            )
        )
        await self.session.flush()
        return published

    async def audit_publish_rejection(
        self, request_id: str, key: str | None, code: str, report_id: UUID | None
    ) -> None:
        self.session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=self.org,
                actor_membership_id=self.actor.membership_id,
                action="report.published",
                outcome=AuditOutcome.REJECTED,
                resource_type="report",
                resource_id=report_id,
                request_id=request_id,
                idempotency_key=key,
                before_data={},
                after_data={},
                reason_data={"reason_code": code},
            )
        )
        await self.session.flush()
