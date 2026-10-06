"""Convert one immutable committed digest; never read live business facts for metrics."""

from contextlib import AbstractAsyncContextManager
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from app.modules.automations.domain.digests import DailySummarySnapshot
from app.modules.identity.domain.auth import AuthenticatedActor

from ..domain.metrics import AggregateReceipt, MetricState, MetricValue, SourceRef
from ..domain.periods import ReportKind, ReportPeriod
from ..domain.reports import ReportCaptureConflict, ReportError, ReportResult
from ..domain.snapshots import ReportMetricSnapshot, canonical_hash


class SummaryReportRepository(Protocol):
    async def authenticate(self) -> None: ...
    async def summary(self, summary_id: UUID) -> DailySummarySnapshot: ...
    async def existing_summary(
        self, summary_id: UUID, locale: str, workflow: str
    ) -> ReportResult | None: ...
    async def save_summary(
        self,
        snapshot: ReportMetricSnapshot,
        summary: DailySummarySnapshot,
        locale: Literal["vi", "en"],
        workflow: str,
    ) -> ReportResult: ...
    async def audit_rejection(
        self, request_id: str, key: str | None, code: str, project_id: UUID | None
    ) -> None: ...


class SummaryReportTransactionPort(Protocol):
    def __call__(
        self, actor: AuthenticatedActor
    ) -> AbstractAsyncContextManager[SummaryReportRepository]: ...


class SummaryReportService:
    def __init__(self, transactions: SummaryReportTransactionPort):
        self.transactions = transactions

    async def ensure_draft(
        self,
        *,
        actor: AuthenticatedActor,
        summary_id: UUID,
        locale: Literal["vi", "en"],
        workflow_version: str,
    ) -> ReportResult:
        if locale not in {"vi", "en"} or workflow_version != "reporting-narrative.v1":
            raise ReportError("VALIDATION_FAILED", 422)
        try:
            for attempt in range(3):
                try:
                    async with self.transactions(actor) as repo:
                        await repo.authenticate()
                        summary = await repo.summary(summary_id)
                        prior = await repo.existing_summary(summary_id, locale, workflow_version)
                        if prior:
                            return prior.model_copy(update={"replayed": True})
                        report_id = uuid5(
                            NAMESPACE_URL,
                            f"summary-report:{actor.organization_id}:{summary_id}:{locale}:{workflow_version}",
                        )
                        snapshot = convert_summary(
                            summary, organization_id=actor.organization_id, report_id=report_id
                        )
                        return await repo.save_summary(snapshot, summary, locale, workflow_version)
                except ReportCaptureConflict:
                    if attempt == 2:
                        raise ReportError("REPORT_CAPTURE_RETRY") from None
            raise ReportError("REPORT_CAPTURE_RETRY")
        except ReportError as exc:
            async with self.transactions(actor) as repo:
                await repo.audit_rejection(str(summary_id), None, exc.code, None)
            raise


def convert_summary(
    summary: DailySummarySnapshot, *, organization_id: UUID, report_id: UUID
) -> ReportMetricSnapshot:
    if summary.scope != "PROJECT":
        raise ReportError("SUMMARY_SCOPE_INVALID", 422)
    snapshot_id = uuid5(NAMESPACE_URL, f"summary-report-snapshot:{report_id}")
    original_hash = canonical_hash(summary.model_dump(mode="json"))
    source = SourceRef(
        resource_type="DAILY_SUMMARY",
        resource_id=summary.id,
        version=1,
        fingerprint=original_hash,
        observed_at=summary.snapshot_at,
        label=summary.project_name,
        facts={
            "project_id": str(summary.project_id),
            "window_id": str(summary.window.id),
            "window": summary.window.model_dump(mode="json"),
            "reporters": [r.model_dump(mode="json") for r in summary.reporters],
            "applied_version": summary.window.applied_version,
            "reason": summary.reason,
            "expected_reporters": [str(v) for v in summary.window.expected_reporters],
            "reported_members": [str(v) for v in summary.window.reported_members],
            "missing_reporters": [str(v) for v in summary.missing_reporters],
            "unknown_inputs": list(summary.unknown_inputs),
        },
    )
    refs = [source]
    for task in summary.tasks:
        refs.append(
            SourceRef(
                resource_type="TASK",
                resource_id=task.id,
                version=task.task_version,
                observed_at=summary.snapshot_at,
                label=task.title,
                facts={
                    "status": task.status,
                    "assignee_id": str(task.assignee_id) if task.assignee_id else None,
                    "reported_percent": task.reported_percent,
                    "remaining_hours": task.remaining_hours,
                    "observation_id": str(task.observation_id) if task.observation_id else None,
                },
            )
        )
    for captured in summary.sources:
        refs.insert(
            1,
            SourceRef(
                resource_type="SUMMARY_SOURCE",
                resource_id=captured.id,
                version=captured.version,
                fingerprint=canonical_hash(captured.model_dump(mode="json")),
                observed_at=summary.snapshot_at,
                label=captured.kind,
                facts={
                    "summary_id": str(summary.id),
                    "summary_hash": original_hash,
                    **captured.model_dump(mode="json"),
                },
            ),
        )
    # Included subtotals are deliberately PARTIAL even if this sample is below its cap.
    metrics: dict[str, MetricValue] = {}

    def metric(
        key: str,
        value: Decimal | int | None,
        unit: Literal["COUNT", "HOURS", "FRACTION", "PERCENT"],
        state: MetricState = MetricState.PARTIAL,
    ):
        metrics[key] = MetricValue(
            key=key,
            value=Decimal(value) if value is not None else None,
            unit=unit,
            state=state,
            time_basis="AT_CAPTURE",
            source_refs=(source,),
            limitations=("INCLUDED_SUMMARY_ITEMS_ONLY",) if state is MetricState.PARTIAL else (),
        )

    metric("included_tasks.status.total_count", len(summary.tasks), "COUNT")
    for status in ("DONE", "IN_PROGRESS", "TO_DO"):
        metric(
            f"included_tasks.status.{status.lower()}_count",
            sum(t.status == status for t in summary.tasks),
            "COUNT",
        )
    observations = [t for t in summary.tasks if t.reported_percent is not None]
    metric("included_progress.observation_count", len(observations), "COUNT")
    metric(
        "included_progress.mean_percent",
        sum(
            (Decimal(t.reported_percent) for t in observations if t.reported_percent is not None),
            Decimal(0),
        )
        / len(observations)
        if observations
        else None,
        "PERCENT",
        MetricState.PARTIAL if observations else MetricState.UNKNOWN,
    )
    for kind in ("BLOCKER", "RISK", "REVIEW", "EVIDENCE"):
        metric(
            f"included_sources.{kind.lower()}_count",
            sum(s.kind == kind for s in summary.sources),
            "COUNT",
        )
    metric("reporting.expected_reporters_count", summary.expected_count, "COUNT", MetricState.KNOWN)
    metric("reporting.reported_reporters_count", summary.reported_count, "COUNT", MetricState.KNOWN)
    metric(
        "reporting.missing_reporters_count",
        len(summary.missing_reporters),
        "COUNT",
        MetricState.KNOWN,
    )
    start = date.fromisoformat(summary.window.local_date)
    period = ReportPeriod(
        kind=ReportKind.DAILY,
        local_start=start,
        local_end=start + timedelta(days=1),
        timezone=summary.window.timezone,
        start_utc=summary.window.starts_at,
        end_utc=summary.window.ends_at,
        observed_through=min(summary.snapshot_at, summary.window.ends_at),
        partial_period=summary.snapshot_at < summary.window.ends_at,
    )
    snapshot = ReportMetricSnapshot(
        id=snapshot_id,
        organization_id=organization_id,
        report_id=report_id,
        project_id=summary.project_id,
        period=period,
        captured_at=summary.snapshot_at,
        query_version="daily-summary-conversion.v1",
        snapshot_hash="0" * 64,
        metrics=metrics,
        sources=tuple(refs),
        receipts=(
            AggregateReceipt(
                id=uuid5(NAMESPACE_URL, f"summary-receipt:{report_id}"),
                project_id=summary.project_id,
                query_version="daily-summary-conversion.v1",
                metric_keys=tuple(metrics),
                row_count=len(summary.tasks),
                captured_at=summary.snapshot_at,
                scope_hash=original_hash,
            ),
        ),
        limitations=(
            "INCLUDED_SUMMARY_ITEMS_ONLY",
            "REPORTER_COVERAGE_IS_NOT_TASK_COMPLETION",
            *summary.unknown_inputs,
        ),
    )
    return snapshot.model_copy(
        update={
            "snapshot_hash": canonical_hash(
                snapshot.model_dump(mode="json", exclude={"snapshot_hash"})
            )
        }
    )
