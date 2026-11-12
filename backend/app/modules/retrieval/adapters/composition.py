"""One trusted retrieval composition shared by API and worker; defaults stay off."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.modules.assistant.adapters.retrieval_runtime import RetrievalRuntime
from app.modules.assistant.adapters.work_tools import CurrentActorResolverPort
from app.modules.assistant.application.ports import AssistantTransactionFactory
from app.modules.optimization.adapters.redis_runtime import RedisRuntime
from app.modules.retrieval.adapters.transaction import (
    PostgreSQLRetrievalTransactions,
    RetrievalActivationPolicy,
)
from app.modules.retrieval.application.admission import RetrievalAdmissionService
from app.modules.retrieval.application.budget_service import RetrievalBudgetService
from app.modules.retrieval.application.ports import (
    PublicDNSResolver,
)
from work_management_ai.runtime.tool_registry import ToolRegistry


def build_retrieval_budgets(
    settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> RetrievalBudgetService:
    return RetrievalBudgetService(
        transactions=PostgreSQLRetrievalTransactions(sessions),
        activation=RetrievalActivationPolicy(lambda: settings),
    )


def build_retrieval_runtime(
    *,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    actors: CurrentActorResolverPort,
    tools: ToolRegistry,
    transactions: AssistantTransactionFactory,
    redis_runtime: RedisRuntime | None = None,
) -> RetrievalRuntime:
    from app.modules.assistant.adapters.retrieval_runtime import RetrievalRuntime
    from app.modules.retrieval.adapters.exa import ExaAdapter, SystemPublicDNSResolver
    from app.modules.retrieval.adapters.mock import MockRetrievalAdapter
    from app.modules.retrieval.application.service import RetrievalService

    admission = None
    if settings.retrieval_admission_enabled:
        if redis_runtime is None or redis_runtime.limiter is None:
            raise ValueError("RETRIEVAL_LIMITER_REQUIRED")
        admission = RetrievalAdmissionService(
            redis_runtime.limiter,
            namespace=redis_runtime.namespace,
            actor_limit=settings.retrieval_admission_actor_limit,
            org_limit=settings.retrieval_admission_org_limit,
        )
    budgets = build_retrieval_budgets(settings, sessions)
    adapter: ExaAdapter | MockRetrievalAdapter
    dns: PublicDNSResolver
    if settings.retrieval_provider == "exa" and settings.exa_api_key:
        adapter = ExaAdapter(settings.exa_api_key)
        dns = SystemPublicDNSResolver()
    else:
        adapter = MockRetrievalAdapter()

        class MockDNS:
            async def resolve(self, hostname: str) -> tuple[str, ...]:
                return ("93.184.216.34",)

        dns = MockDNS()
    return RetrievalRuntime(
        budgets=budgets,
        service=RetrievalService(
            search=adapter,
            pages=adapter,
            access=budgets,
            dns=dns,
            admission=admission,
        ),
        actors=actors,
        tools=tools,
        transactions=transactions,
    )
