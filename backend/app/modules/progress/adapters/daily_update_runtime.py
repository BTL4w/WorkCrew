"""Shared API/worker composition of Phase 4 application services."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.modules.assistant.adapters.daily_update_usage import SqlAlchemyDailyUsageStore
from app.modules.progress.adapters.assessment_repository import SqlAlchemyAssessmentTransactions
from app.modules.progress.adapters.daily_update_comparison import OriginalEvidenceComparison
from app.modules.progress.adapters.daily_update_repository import SqlAlchemyDailyUpdateTransactions
from app.modules.progress.application.assessment_service import AssessmentService, ComparisonBudget
from app.modules.progress.application.daily_update_service import DailyUpdateService
from app.modules.progress.application.evidence_service import EvidenceService
from work_management_ai.model_gateway.contracts import ModelGateway
from work_management_ai.runtime.budgeted_gateway import BudgetedDailyGateway


def image_token_bound(settings: Settings) -> int | None:
    if settings.ai_provider == "mock":
        return 6000
    # Versioned token admission policy. Other model families fail closed for media;
    # original transport remains untouched. See phase-4 Daily Update runbook.
    if settings.ai_provider == "openai" and (
        settings.ai_model in {"gpt-4o", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano"}
        or settings.ai_model.startswith(
            ("gpt-4o-2024-", "gpt-4.1-2025-", "gpt-4.1-mini-2025-", "gpt-4.1-nano-2025-")
        )
    ):
        return 6000
    return None


def build_daily_services(
    *,
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    evidence: EvidenceService,
    gateway: ModelGateway,
) -> tuple[DailyUpdateService, AssessmentService, SqlAlchemyDailyUsageStore]:
    store = SqlAlchemyDailyUsageStore(sessions)
    budgeted = BudgetedDailyGateway(gateway, store, image_token_bound=image_token_bound(settings))
    assessments = AssessmentService(
        SqlAlchemyAssessmentTransactions(sessions),
        budget=ComparisonBudget(timeout_seconds=60),
        comparison_factory=lambda actor, run_id: OriginalEvidenceComparison(
            gateway=budgeted, evidence=evidence, actor=actor, run_id=run_id
        ),
    )
    updates = DailyUpdateService(
        SqlAlchemyDailyUpdateTransactions(sessions),
        settings.reporting_timezone,
        assessment_available=settings.ai_provider != "disabled",
    )
    return updates, assessments, store
