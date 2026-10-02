"""Assessment and submit share the same draft row lock and tenant authorization."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.planning_runs.adapters.database_models import OutboxEventModel
from app.modules.progress.adapters.assessment_models import (
    EvidenceAssessmentModel,
    WarningAcknowledgmentModel,
)
from app.modules.progress.adapters.daily_update_repository import SqlAlchemyDailyUpdateRepository
from app.modules.progress.adapters.evidence_models import EvidenceOriginalModel
from app.modules.progress.application.assessment_service import (
    AssessmentRepository,
    CriterionContext,
    OriginalSource,
    TaskAssessmentContext,
)
from app.modules.progress.domain.daily_updates import (
    ConfirmDailyUpdateCommand,
    DailyUpdateDraft,
    DailyUpdateError,
)
from app.modules.progress.domain.evidence_support import DraftAssessment
from app.modules.work.planning.adapters.database_models import AcceptanceCriterionModel


class SqlAlchemyAssessmentRepository(SqlAlchemyDailyUpdateRepository):
    def event(self, assessment: DraftAssessment, event_type: str) -> None:
        event_id = uuid4()
        self.session.add(
            OutboxEventModel(
                id=event_id,
                event_id=event_id,
                organization_id=self.org,
                event_type=event_type,
                aggregate_type="evidence_assessment",
                aggregate_id=assessment.id,
                envelope_version="1.0",
                payload={
                    "assessment_id": str(assessment.id),
                    "draft_id": str(assessment.draft_id),
                    "draft_version": assessment.draft_version,
                    "state": assessment.state,
                },
            )
        )

    async def binding_for(
        self, draft: DailyUpdateDraft, *, lock: bool = False
    ) -> tuple[dict[str, object], tuple[OriginalSource, ...]]:
        criteria: dict[str, object] = {}
        originals: dict[tuple[UUID, int], OriginalSource] = {}
        for item in draft.items:
            task = await self._task(item.task_id)
            if task.version != item.expected_task_version:
                raise DailyUpdateError("STALE_TASK")
            await self._evidence(item, datetime.now(UTC), lock=lock)
            criteria_query = (
                select(AcceptanceCriterionModel)
                .where(
                    AcceptanceCriterionModel.organization_id == self.org,
                    AcceptanceCriterionModel.task_id == item.task_id,
                )
                .order_by(AcceptanceCriterionModel.id)
            )
            rows = (
                await self.session.scalars(
                    criteria_query.with_for_update() if lock else criteria_query
                )
            ).all()
            criteria[str(item.task_id)] = {str(row.id): row.version for row in rows}
            for ref in item.evidence_refs:
                original = await self.session.scalar(
                    select(EvidenceOriginalModel).where(
                        EvidenceOriginalModel.organization_id == self.org,
                        EvidenceOriginalModel.id == ref.evidence_id,
                        EvidenceOriginalModel.version == ref.version,
                    )
                )
                assert original is not None
                previous = originals.get((ref.evidence_id, ref.version))
                originals[(ref.evidence_id, ref.version)] = OriginalSource(
                    evidence_id=ref.evidence_id,
                    version=ref.version,
                    sha256=original.sha256 or "",
                    mime_type=original.mime_type or "",
                    task_contexts=(previous.task_contexts if previous else ())
                    + (
                        TaskAssessmentContext(
                            task_id=task.id,
                            task_version=task.version,
                            title=task.title,
                            description=task.description or "",
                            criteria=tuple(
                                CriterionContext(id=row.id, version=row.version, text=row.text)
                                for row in rows
                            ),
                        ),
                    ),
                )
        sources = tuple(originals[key] for key in sorted(originals))
        return {
            "content_hash": draft.content_hash,
            "tasks": {str(i.task_id): i.expected_task_version for i in draft.items},
            "criteria": criteria,
            "sources": [s.model_dump(mode="json") for s in sources],
            "rule_version": "evidence-support.v1",
        }, sources

    async def latest_row(self, draft: DailyUpdateDraft) -> EvidenceAssessmentModel | None:
        return await self.session.scalar(
            select(EvidenceAssessmentModel)
            .where(
                EvidenceAssessmentModel.organization_id == self.org,
                EvidenceAssessmentModel.draft_id == draft.id,
                EvidenceAssessmentModel.draft_version == draft.version,
            )
            .order_by(EvidenceAssessmentModel.attempt.desc())
            .limit(1)
            .with_for_update()
        )

    async def current(self, draft: DailyUpdateDraft) -> DraftAssessment:
        current_binding, _ = await self.binding_for(draft)
        row = await self.latest_row(draft)
        if row is None:
            return DraftAssessment(
                draft_id=draft.id,
                draft_version=draft.version,
                state="UNAVAILABLE",
                limitation="COMPARISON_NOT_REQUESTED",
            )
        if row.state == "PENDING" and row.deadline <= datetime.now(UTC):
            result = DraftAssessment.model_validate(row.payload).model_copy(
                update={"state": "UNAVAILABLE", "limitation": "ASSESSMENT_TIMEOUT"}
            )
            row.state = "UNAVAILABLE"
            row.payload = result.model_dump(mode="json")
            self.event(result, "daily_update.assessment_finished")
            await self.audit(
                "daily_update.assessment_timed_out", f"assessment-timeout:{row.id}", None, draft.id
            )
            await self.session.flush()
        result = DraftAssessment.model_validate(row.payload)
        if row.binding != current_binding:
            return result.model_copy(
                update={
                    "state": "STALE",
                    "result": None,
                    "warnings": (),
                    "findings": (),
                    "limitation": "STALE_ASSESSMENT",
                }
            )
        return result

    async def start(
        self, draft: DailyUpdateDraft
    ) -> tuple[DraftAssessment, tuple[OriginalSource, ...]]:
        binding, sources = await self.binding_for(draft)
        await self.current(draft)  # terminalize expired attempts before appending a retry
        previous = await self.latest_row(draft)
        if previous and previous.state == "PENDING" and previous.deadline > datetime.now(UTC):
            raise DailyUpdateError("ASSESSMENT_PENDING")
        result = DraftAssessment(
            id=uuid4(), draft_id=draft.id, draft_version=draft.version, state="PENDING"
        )
        self.session.add(
            EvidenceAssessmentModel(
                id=result.id,
                organization_id=self.org,
                draft_id=draft.id,
                draft_version=draft.version,
                attempt=(previous.attempt + 1 if previous else 1),
                owner_membership_id=self.actor.membership_id,
                content_hash=draft.content_hash,
                binding=binding,
                state=result.state,
                payload=result.model_dump(mode="json"),
                created_at=datetime.now(UTC),
                deadline=datetime.now(UTC) + timedelta(seconds=60),
            )
        )
        self.event(result, "daily_update.assessment_requested")
        await self.session.flush()
        return result, sources

    async def finish(self, draft: DailyUpdateDraft, result: DraftAssessment) -> DraftAssessment:
        current = await self.draft(draft.id)  # same lock used by revision/confirmation
        row = await self.session.scalar(
            select(EvidenceAssessmentModel)
            .where(
                EvidenceAssessmentModel.organization_id == self.org,
                EvidenceAssessmentModel.id == result.id,
            )
            .with_for_update()
        )
        if row is None:
            raise DailyUpdateError("RESOURCE_NOT_FOUND", 404)
        if row.state != "PENDING":
            return DraftAssessment.model_validate(row.payload)
        if current.version != draft.version or current.confirmed_update_id:
            raise DailyUpdateError("STALE_DRAFT")
        binding, _ = await self.binding_for(current)
        if binding != row.binding:
            raise DailyUpdateError("STALE_ASSESSMENT")
        row.state = result.state
        row.payload = result.model_dump(mode="json")
        self.event(result, "daily_update.assessment_finished")
        await self.session.flush()
        return result

    async def validate_confirmation(
        self,
        draft: DailyUpdateDraft,
        command: ConfirmDailyUpdateCommand,
        *,
        assessment_available: bool = False,
    ) -> DraftAssessment:
        assessment = await self.current(draft)
        details: dict[str, object] = {"assessment": assessment.model_dump(mode="json")}
        if assessment.state == "STALE":
            raise DailyUpdateError("STALE_ASSESSMENT", details=details)
        if (
            assessment_available
            and assessment.id is None
            and any(i.evidence_refs for i in draft.items)
        ):
            raise DailyUpdateError("ASSESSMENT_REQUIRED", details=details)
        if assessment.state == "PENDING":
            raise DailyUpdateError("ASSESSMENT_PENDING", details=details)
        if assessment.id != command.assessment_id:
            if assessment.id is None and draft.version == 1:
                raise DailyUpdateError("ASSESSMENT_UNAVAILABLE", 422)
            raise DailyUpdateError("STALE_ASSESSMENT", details=details)
        if set(command.warning_acknowledgments) != {w.id for w in assessment.warnings} or len(
            set(command.warning_acknowledgments)
        ) != len(command.warning_acknowledgments):
            raise DailyUpdateError("WARNING_ACKNOWLEDGMENT_REQUIRED", details=details)
        if assessment.id is not None:
            row = await self.latest_row(draft)
            assert row is not None
            binding, _ = await self.binding_for(draft, lock=True)
            if binding != row.binding:
                raise DailyUpdateError("STALE_ASSESSMENT", details=details)
        return assessment

    async def record_confirmation(self, assessment: DraftAssessment, update_id: UUID) -> None:
        if assessment.id is not None:
            row = await self.latest_row(await self.draft(assessment.draft_id))
            assert row is not None
            self.session.add(
                WarningAcknowledgmentModel(
                    id=uuid4(),
                    organization_id=self.org,
                    update_id=update_id,
                    assessment_id=assessment.id,
                    owner_membership_id=self.actor.membership_id,
                    acknowledged_at=datetime.now(UTC),
                    payload={
                        "assessment": assessment.model_dump(mode="json"),
                        "binding": row.binding,
                    },
                )
            )


class SqlAlchemyAssessmentTransactions:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self.sessions = sessions

    @asynccontextmanager
    async def __call__(self, actor: AuthenticatedActor) -> AsyncGenerator[AssessmentRepository]:
        async with self.sessions.begin() as session:
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true), "
                    "set_config('app.membership_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            yield SqlAlchemyAssessmentRepository(session, actor)
