"""Daily Update tools reuse owner-authorized application transactions."""

import re
from typing import Literal, cast
from uuid import UUID

from app.modules.assistant.adapters.assignment_tools import CurrentActorResolverPort
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.progress.application.assessment_service import AssessmentService
from app.modules.progress.application.daily_update_service import DailyUpdateService
from app.modules.progress.domain.blockers import BlockerCommand
from app.modules.progress.domain.daily_updates import (
    DailyUpdateError,
    DailyUpdateItemInput,
    SelectedEvidence,
)
from app.modules.work.application.task_service import TaskService
from work_management_ai.agents.daily_update.contracts import (
    DailyUpdateHandoff,
    DailyUpdateResult,
    EvidenceRef,
    ReportingSnapshot,
)
from work_management_ai.runtime.contracts import (
    JsonValue,
    ToolExecutionRequest,
    ToolExecutionResult,
)
from work_management_ai.tools.daily_update.contracts import DailyUpdateToolInput


class DailyUpdateToolAdapter:
    def __init__(
        self,
        *,
        actors: CurrentActorResolverPort,
        updates: DailyUpdateService,
        assessments: AssessmentService,
    ):
        self.actors = actors
        self.updates = updates
        self.assessments = assessments

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        try:
            if request.tool_id != "daily_update.prepare" or request.tool_version != "1.0.0":
                raise ValueError("TOOL_NOT_ALLOWED")
            actor = await self.actors.resolve(
                organization_id=request.actor.organization_id,
                membership_id=request.actor.membership_id,
            )
            if actor is None:
                raise ValueError("ACTOR_INACTIVE")
            value = DailyUpdateToolInput.model_validate(request.typed_input)
            context = await self.updates.context(actor, value.task_id)
            if context.task_version != value.task_version:
                raise DailyUpdateError("STALE_TASK")
            if value.action == "CONTEXT":
                if value.draft_id:
                    draft = await self.updates.get_draft(actor, value.draft_id)
                    if (
                        draft.version != value.draft_version
                        or draft.confirmed_update_id
                        or len(draft.items) != 1
                        or draft.items[0].task_id != value.task_id
                    ):
                        raise DailyUpdateError("STALE_DRAFT")
                    if tuple(ref.model_dump() for ref in value.evidence_refs) != tuple(
                        ref.model_dump() for ref in draft.items[0].evidence_refs
                    ):
                        raise DailyUpdateError("STALE_EVIDENCE")
                result = ReportingSnapshot(
                    task_id=context.task_id,
                    task_version=context.task_version,
                    progress_version=context.progress_version,
                    reporting_date=context.reporting_date,
                    evidence_refs=value.evidence_refs,
                )
            elif value.action == "DRAFT":
                report = value.report
                if report is None or report.needs_clarification or report.reported_percent is None:
                    raise ValueError("DAILY_UPDATE_INPUT_REQUIRED")
                item = DailyUpdateItemInput(
                    task_id=value.task_id,
                    expected_task_version=context.task_version,
                    expected_progress_version=context.progress_version,
                    reporting_date=context.reporting_date,
                    reported_percent=report.reported_percent,
                    remaining_hours=report.remaining_hours,
                    spent_hours=report.spent_hours,
                    done_text=report.done_text,
                    next_steps=report.next_steps,
                    blocker_commands=tuple(
                        BlockerCommand(
                            task_id=value.task_id,
                            expected_task_version=context.task_version,
                            action="CREATE",
                            text=blocker.text,
                            severity=blocker.severity,
                            evidence_refs=tuple(
                                SelectedEvidence.model_validate(r.model_dump())
                                for r in value.evidence_refs
                            ),
                        )
                        for blocker in report.blockers
                    ),
                    evidence_refs=tuple(
                        SelectedEvidence.model_validate(r.model_dump()) for r in value.evidence_refs
                    ),
                )
                draft = await self.updates.create_draft(
                    actor,
                    (item,),
                    request.idempotency_key,
                    request.call_id,
                    draft_id=value.draft_id,
                    expected_version=value.draft_version,
                )
                result = DailyUpdateResult(
                    draft_id=draft.id,
                    draft_version=draft.version,
                    task_id=value.task_id,
                    task_version=context.task_version,
                    assessment_id=None,
                )
            else:
                if value.draft_id is None or value.draft_version is None:
                    raise ValueError("DAILY_UPDATE_INPUT_REQUIRED")
                draft = await self.updates.get_draft(actor, value.draft_id)
                if len(draft.items) != 1 or draft.items[0].task_id != value.task_id:
                    raise DailyUpdateError("STALE_DRAFT")
                ref = await self.assessments.assess(
                    actor, draft.id, value.draft_version, request.idempotency_key, request.call_id
                )
                result = DailyUpdateResult(
                    draft_id=draft.id,
                    draft_version=draft.version,
                    task_id=value.task_id,
                    task_version=context.task_version,
                    assessment_id=ref.id,
                )
            return ToolExecutionResult(
                status="SUCCEEDED",
                typed_output=cast(dict[str, JsonValue], result.model_dump(mode="json")),
            )
        except DailyUpdateError as error:
            return ToolExecutionResult(
                status="REJECTED", typed_output={}, safe_error_code=error.code
            )
        except Exception:
            return ToolExecutionResult(
                status="REJECTED", typed_output={}, safe_error_code="DAILY_UPDATE_TOOL_REJECTED"
            )


class DailyUpdateContextResolver:
    """Resolve exact visible Task titles; ambiguity never selects a Task by guess."""

    DRAFT_PATTERN = re.compile(
        r"^\s*(?:daily update draft|cập nhật bản nháp)\s+"
        r"([0-9a-f-]{36})\s*:\s*(.+)$",
        re.I | re.S,
    )
    PATTERN = re.compile(
        r"^\s*(?:daily update|cập nhật hôm nay|cập nhật hằng ngày)\s+"
        r"(?:for|cho)\s+(.+?)\s*:\s*(.+)$",
        re.I | re.S,
    )

    def __init__(self, *, tasks: TaskService, updates: DailyUpdateService):
        self.tasks = tasks
        self.updates = updates

    async def resolve(
        self, *, actor: AuthenticatedActor, message: str, locale: Literal["vi", "en"]
    ) -> tuple[DailyUpdateHandoff | None, bool]:
        draft_match = self.DRAFT_PATTERN.match(message)
        if draft_match is not None:
            identifier, report = draft_match.groups()
            try:
                draft = await self.updates.get_draft(actor, UUID(identifier))
                if draft.confirmed_update_id or len(draft.items) != 1:
                    return None, True
                item = draft.items[0]
                context = await self.updates.context(actor, item.task_id)
                if context.task_version != item.expected_task_version:
                    return None, True
                return DailyUpdateHandoff(
                    text=report,
                    locale=locale,
                    task_id=item.task_id,
                    task_version=context.task_version,
                    draft_id=draft.id,
                    draft_version=draft.version,
                    evidence_refs=tuple(
                        EvidenceRef.model_validate(ref.model_dump()) for ref in item.evidence_refs
                    ),
                ), False
            except (DailyUpdateError, ValueError):
                return None, True
        match = self.PATTERN.match(message)
        if match is None:
            return None, False
        title, text = match.groups()
        visible = await self.tasks.find_visible_tasks_by_title(
            actor=actor, query=title.strip(), limit=20
        )
        exact = tuple(t for t in visible if t.title.casefold() == title.strip().casefold())
        if len(exact) != 1:
            return None, True
        task = exact[0]
        try:
            await self.updates.context(actor, task.id)
        except DailyUpdateError:
            return None, True
        return DailyUpdateHandoff(
            text=text, locale=locale, task_id=task.id, task_version=task.version
        ), False
