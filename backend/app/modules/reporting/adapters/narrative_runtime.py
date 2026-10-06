"""Explicit JSON adapters preserve the verified immutable snapshot across packages."""

from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.assistant.adapters.agent_runtime import CurrentActorResolverPort
from app.modules.assistant.application.ports import AssistantTransaction
from app.modules.identity.domain.auth import AuthenticatedActor
from work_management_ai.agents.reporting.contracts import ReportingSnapshot, ReportingUsageScope
from work_management_ai.agents.reporting.workflows.graph import NODES
from work_management_ai.model_gateway.contracts import (
    ModelGateway,
    StructuredModelRequest,
    StructuredModelResponse,
)

from ..domain.generation import GenerationJob
from ..domain.snapshots import ReportMetricSnapshot


def to_reporting_snapshot(snapshot: ReportMetricSnapshot) -> ReportingSnapshot:
    if not snapshot.verified_hash():
        raise ValueError("SNAPSHOT_HASH_MISMATCH")
    value = ReportingSnapshot.model_validate(snapshot.model_dump(mode="json"))
    if not value.verified_hash():
        raise ValueError("SNAPSHOT_WIRE_DRIFT")
    return value


def to_domain_snapshot(snapshot: ReportingSnapshot) -> ReportMetricSnapshot:
    value = ReportMetricSnapshot.model_validate(snapshot.model_dump(mode="json"))
    if not snapshot.verified_hash() or not value.verified_hash():
        raise ValueError("SNAPSHOT_WIRE_DRIFT")
    return value


class ReportNarrativeRuntime:
    """Worker intent enters the hub; the specialist cannot delegate or approve."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        actors: CurrentActorResolverPort,
        gateway: ModelGateway,
        timezone: str,
    ):
        self.sessions, self.actors, self.gateway, self.timezone = (
            sessions,
            actors,
            gateway,
            timezone,
        )

    async def execute(self, job: GenerationJob, scope: ReportingUsageScope) -> bool:
        from app.modules.assistant.adapters.agent_runtime import (
            InactivePlanningToolExecutor,
            build_agent_registry,
            build_execution_engine_factory,
        )
        from app.modules.assistant.adapters.execution_recorder import PostgreSQLTriggerRecorder
        from app.modules.assistant.adapters.reporting_tools import ReportingToolAdapter
        from app.modules.assistant.adapters.transaction import PostgreSQLAssistantTransactionFactory
        from app.modules.assistant.adapters.trigger_repository import TriggerTransactions
        from app.modules.assistant.adapters.work_tools import RecordingToolExecutor
        from app.modules.assistant.application.trigger_service import TriggerService
        from app.modules.organization.domain.roles import MembershipRole
        from work_management_ai.agents.orchestrator.contracts import OrchestratorTriggerInput
        from work_management_ai.runtime.contracts import ActorReference
        from work_management_ai.runtime.triggers import ExecutionScope, ReportRequestTrigger

        from ..application.generation_service import GenerationService
        from .generation_repository import GenerationTransactions
        from .usage import ReportingUsageStore, ScopedReportingUsage

        actor = await self.actors.resolve(
            organization_id=job.organization_id, membership_id=job.requester_membership_id
        )
        if actor is None or actor.role not in (MembershipRole.MANAGER, MembershipRole.ADMIN):
            return False
        transactions = GenerationTransactions(self.sessions, self.timezone)
        service = GenerationService(transactions)
        # A committed proposal is the replay boundary, even if recorder completion was interrupted.
        from .transaction import ReportTransactions

        async with ReportTransactions(self.sessions, self.timezone)(actor) as reports:
            await reports.authenticate()
            result = await reports.get(job.report_id)
        recovering = job.proposed_version_id is not None
        if recovering and (
            result.selected_version.id != job.proposed_version_id
            or result.snapshot.snapshot_hash != job.snapshot_hash
            or result.selected_version.generation_id
            != (job.original_generation_id if job.job_type == "EDIT_VERIFICATION" else job.id)
            or result.selected_version.narrative is None
        ):
            return False
        trigger = ReportRequestTrigger(
            report_id=job.report_id,
            base_version_id=job.base_version_id,
            snapshot_hash=job.snapshot_hash,
            request_key=job.request_key,
            mode="VERIFY_EDIT" if job.job_type == "EDIT_VERIFICATION" else "DRAFT",
            edited_version_id=job.base_version_id if job.job_type == "EDIT_VERIFICATION" else None,
        )
        registry, tools = build_agent_registry()
        triggers = TriggerService(TriggerTransactions(self.sessions), registry)
        run = await triggers.ensure(actor=actor, trigger=trigger)
        if recovering:
            if run.id != job.orchestration_run_id:
                return False
        else:
            async with transactions(actor) as repo:
                await repo.attach_run(scope, run.id)
        await triggers.start(actor=actor, run_id=run.id)
        base_transactions = PostgreSQLAssistantTransactionFactory(self.sessions)

        def assistant_transactions(
            context: AuthenticatedActor | UUID,
        ) -> AssistantTransaction:
            org = context.organization_id if isinstance(context, AuthenticatedActor) else context
            if org != actor.organization_id:
                raise ValueError("REPORTING_TRANSACTION_SCOPE")
            return base_transactions(actor)

        recorder = PostgreSQLTriggerRecorder(
            transaction_factory=assistant_transactions,
            registry=registry,
            actor=actor,
            scope=ExecutionScope(
                organization_id=actor.organization_id,
                actor_membership_id=actor.membership_id,
                orchestration_run_id=run.id,
                trigger=trigger,
            ),
        )
        hub_id = await recorder.ensure_orchestrator_run()
        if recovering:
            from typing import cast

            from sqlalchemy import select

            from .generation_repository import SQLGenerationRepository
            from .usage_models import ReportGenerationUsageModel

            async with transactions(actor) as port:
                usage_row = await cast(SQLGenerationRepository, port).session.scalar(
                    select(ReportGenerationUsageModel).where(
                        ReportGenerationUsageModel.organization_id == job.organization_id,
                        ReportGenerationUsageModel.generation_id == job.id,
                    )
                )
                if usage_row is None:
                    return False
                attempts, tool_calls = usage_row.attempts, usage_row.tools

            from work_management_ai.runtime.contracts import (
                AgentId,
                AgentResult,
                AgentRunStatus,
                JsonValue,
            )

            version = result.selected_version
            assert version.narrative is not None
            await recorder.finish_agent_run(
                uuid5(NAMESPACE_URL, f"agent-run:{run.id}:reporting"),
                AgentResult(
                    agent_id=AgentId.REPORTING,
                    agent_version="1.0.0",
                    status=AgentRunStatus.AWAITING_HUMAN,
                    typed_output=cast(
                        dict[str, JsonValue],
                        {
                            "narrative": version.narrative.model_dump(mode="json"),
                            "version_id": str(version.id),
                        },
                    ),
                    stop_reason="AWAITING_MANAGER_REVIEW",
                    model_attempts_used=attempts,
                    tool_calls_used=tool_calls,
                    iterations_used=len(NODES),
                ),
            )
        executor = RecordingToolExecutor(
            transaction_factory=assistant_transactions,
            tool_registry=tools,
            backend=ReportingToolAdapter(actors=self.actors, service=service, scope=scope),
        )
        engine = build_execution_engine_factory(
            model_gateway=_RecoveryGateway() if recovering else self.gateway,
            registry=registry,
            actor_resolver=self.actors,
            work_tool_executor=InactivePlanningToolExecutor(),
            transaction_factory=assistant_transactions,
            reporting_tool_executor=executor,
            reporting_usage=ScopedReportingUsage(ReportingUsageStore(self.sessions), scope),
        )(recorder)
        output = await engine.execute_trigger(
            value=OrchestratorTriggerInput(
                actor=ActorReference(
                    organization_id=actor.organization_id, membership_id=actor.membership_id
                ),
                orchestration_run_id=run.id,
                trigger=trigger,
                locale=result.report.locale,
            ),
            recorder=recorder,
        )
        await recorder.finish_orchestrator_run(hub_id, output)
        succeeded = output.status.value == "AWAITING_HUMAN"
        from sqlalchemy import select

        from .usage_models import ReportGenerationUsageModel

        async with transactions(actor) as port:
            from typing import cast

            from .generation_repository import SQLGenerationRepository

            row = await cast(SQLGenerationRepository, port).session.scalar(
                select(ReportGenerationUsageModel).where(
                    ReportGenerationUsageModel.organization_id == job.organization_id,
                    ReportGenerationUsageModel.generation_id == job.id,
                )
            )
            if row is None:
                return False
            usage = {
                "model_attempts": row.attempts,
                "input_reserved": row.input_reserved,
                "output_reserved": row.output_reserved,
                "tool_calls": row.tools,
                "transient_retries": row.retries,
            }
        await triggers.finish(actor=actor, run_id=run.id, succeeded=succeeded, usage=usage)
        return succeeded


class _RecoveryGateway:
    """Reconciliation cannot issue a new model call, even if recorder state is corrupt."""

    async def generate_structured[T: BaseModel](
        self, request: StructuredModelRequest[T]
    ) -> StructuredModelResponse[T]:
        raise RuntimeError("REPORTING_RECOVERY_MODEL_DENIED")
