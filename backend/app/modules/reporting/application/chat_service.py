"""Project resolution and operational status snapshots; no publication authority."""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.work.application.project_service import ProjectService
from app.modules.work.domain.projects import ProjectNotFoundError
from work_management_ai.agents.orchestrator.contracts import ReportIntent
from work_management_ai.agents.reporting.contracts import ReportingSnapshot
from work_management_ai.runtime.contracts import ProjectStatusResponseBlock, ReportResponseBlock

from ..domain.commands import CaptureReportCommand, CreateReportCommand
from ..domain.periods import ReportKind, normalize_period
from ..domain.reports import ReportError
from .report_service import ReportService
from .snapshot_service import ReportSnapshotService


def summary(snapshot: Any, label: str) -> dict[str, Any]:
    keys = (
        "tasks.status.total_count",
        "tasks.status.done_count",
        "tasks.status.in_progress_count",
        "tasks.status.to_do_count",
        "progress.observation_coverage",
        "blockers.open_count",
    )
    metrics = [
        dict(
            key=key,
            value=str(snapshot.metrics[key].value)
            if snapshot.metrics[key].value is not None
            else None,
            unit=snapshot.metrics[key].unit,
            state=snapshot.metrics[key].state,
            time_basis=snapshot.metrics[key].time_basis,
        )
        for key in keys
        if key in snapshot.metrics
    ]
    return dict(
        project_id=snapshot.project_id,
        project_label=label[:200],
        snapshot_id=snapshot.id,
        snapshot_hash=snapshot.snapshot_hash,
        period_start=snapshot.period.local_start.isoformat(),
        period_end=snapshot.period.local_end.isoformat(),
        timezone=snapshot.period.timezone,
        report_kind=snapshot.period.kind,
        captured_at=snapshot.captured_at,
        metrics=metrics,
        sources=[
            dict(resource_type=r.resource_type, resource_id=r.resource_id, version=r.version)
            for r in snapshot.sources[:8]
        ],
        limitations=list(snapshot.limitations[:10]),
    )


class ReportChatService:
    def __init__(self, *, reports: ReportService, projects: ProjectService):
        self.reports, self.projects = reports, projects

    async def prepare(
        self,
        *,
        actor: AuthenticatedActor,
        intent: dict[str, Any],
        key: str,
        context_run_id: UUID | None = None,
    ) -> dict[str, Any]:
        value = ReportIntent.model_validate(intent)
        if actor.role.value not in {"ADMIN", "MANAGER"}:
            raise ReportError("FORBIDDEN", 403)
        try:
            project_id = UUID(value.project_reference)
            project = await self.projects.get_project(actor=actor, project_id=project_id)
        except ValueError:
            page = await self.projects.list_projects(
                actor=actor, query=value.project_reference, page=1, page_size=20
            )
            if page.total != 1:
                return dict(
                    resolution="AMBIGUOUS" if page.total else "NOT_FOUND",
                    candidates=[dict(id=str(p.id), name=p.name[:200]) for p in page.items],
                )
            project = page.items[0]
        except ProjectNotFoundError:
            return dict(resolution="NOT_FOUND", candidates=[])
        defaults = await self.reports.defaults(actor=actor, project_id=project.id)
        timezone = value.timezone or defaults.timezone
        async with self.reports.transactions(actor) as repo:
            await repo.authenticate()
            await repo.authorize_project(project.id)
            at = await repo.captured_at()
        start = value.period_start or at.astimezone(ZoneInfo(timezone)).date()
        if value.period_start is None:
            if value.kind == "WEEKLY":
                start -= timedelta(days=start.weekday())
            if value.relative_period == "PREVIOUS":
                start -= timedelta(days=7 if value.kind == "WEEKLY" else 1)
        if value.operation == "PREPARE_REPORT":
            result = await self.reports.create(
                actor=actor,
                command=CreateReportCommand(
                    project_id=project.id,
                    kind=ReportKind(value.kind),
                    period_start=start,
                    timezone=timezone,
                    locale=value.locale,
                ),
                idempotency_key=key,
            )
            card = ReportResponseBlock.model_validate(
                {
                    **summary(result.snapshot, project.name),
                    "context_run_id": context_run_id or uuid4(),
                    "report_id": result.report.id,
                    "report_version_id": result.selected_version.id,
                    "generation_state": str(result.generation_state),
                    "href": (
                        f"/?project={project.id}&report={result.report.id}"
                        f"&version={result.selected_version.id}"
                    ),
                }
            )
            return dict(resolution="UNIQUE", card=card.model_dump(mode="json"))
        async with self.reports.transactions(actor) as repo:
            await repo.authenticate()
            await repo.authorize_project(project.id)
            captured = await repo.captured_at()
            snapshot = await ReportSnapshotService(repo.snapshot_reader).capture(
                CaptureReportCommand(
                    report_id=uuid4(),
                    snapshot_id=uuid4(),
                    project_id=project.id,
                    captured_at=captured,
                    period=normalize_period(ReportKind(value.kind), start, timezone, captured),
                )
            )
        # Operational snapshot is recorded by the existing Tool Invocation, never reports.
        card = ProjectStatusResponseBlock.model_validate(
            {**summary(snapshot, project.name), "context_run_id": context_run_id or uuid4()}
        )
        return dict(
            resolution="UNIQUE",
            card=card.model_dump(mode="json"),
            context=dict(
                snapshot=ReportingSnapshot.model_validate(
                    snapshot.model_dump(mode="json")
                ).model_dump(mode="json"),
                base_version_id=str(uuid4()),
                locale=value.locale,
                project_label=project.name[:200],
            ),
        )
