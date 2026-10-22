"""Progressive graph composition; authorization, counters and checkpoints are durable."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import select

from app.core.config import Settings
from app.modules.assistant.adapters.retrieval_planning import PlanningRetrievalContext
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.planning_runs.adapters.database_models import PlanningDraftModel
from app.modules.planning_runs.adapters.draft_repository import (
    PostgreSQLPlanningDraftRepository,
    build_draft_transaction_factory,
    lock_planning_workflow,
)
from app.modules.planning_runs.application.draft_service import PlanningDraftService
from app.modules.planning_runs.application.ports import PlanningRunTransaction
from app.modules.planning_runs.domain.drafts import DraftContent
from app.modules.planning_runs.domain.models import (
    WorkflowCheckpoint,
    WorkflowJob,
    WorkflowRun,
    WorkflowRunStatus,
)
from work_management_ai.model_gateway.contracts import (
    ModelGateway,
    StructuredModelRequest,
    StructuredModelResponse,
)
from work_management_ai.model_gateway.errors import ModelInvalidInputError
from work_management_ai.schemas.progressive_planning import (
    DraftBinding,
    PlanningDecision,
    PlanningDraft,
    PlanningQuestion,
)
from work_management_ai.workflows.planning.context import (
    PermittedPlanningContext,
    PlanningContextRequest,
)
from work_management_ai.workflows.planning.draft_verifier import (
    DraftValidationResult,
    planning_source_references,
)
from work_management_ai.workflows.planning.graph import PlanningGraph
from work_management_ai.workflows.planning.ports import (
    PersistedProposalReference,
    PlanningCheckpoint,
    PlanningProgressEvent,
    PlanningProposalDraft,
)
from work_management_ai.workflows.planning.state import (
    PlanningLocale,
    ProgressivePlanningState,
)

TransactionFactory = Callable[[AuthenticatedActor | UUID], PlanningRunTransaction]


class ProgressiveContext:
    def __init__(
        self,
        transactions: TransactionFactory,
        actor: AuthenticatedActor,
        run: WorkflowRun,
        job: WorkflowJob,
        refs: tuple[str, ...],
        retrieval_context: PlanningRetrievalContext | None = None,
    ) -> None:
        self.retrieval_context = retrieval_context
        self.transactions, self.actor, self.run, self.job, self.refs = (
            transactions,
            actor,
            run,
            job,
            refs,
        )

    async def load_permitted_context(
        self, request: PlanningContextRequest
    ) -> PermittedPlanningContext:
        if (request.organization_id, request.actor_membership_id, request.run_id) != (
            self.actor.organization_id,
            self.actor.membership_id,
            self.run.id,
        ):
            raise PermissionError("planning context unavailable")
        async with self.transactions(self.actor) as transaction:
            await PostgreSQLPlanningDraftRepository(transaction).authorize(self.actor, self.run.id)
        facts = (
            await self.retrieval_context.load(self.actor, self.run.id)
            if self.retrieval_context is not None
            else {}
        )
        return PermittedPlanningContext(
            (
                *self.refs,
                *(
                    f"web:{source['evidence_id']}:{source['fingerprint']}"
                    for source in facts.get("untrusted_web_evidence", [])
                ),
            ),
            (),
            {
                **facts,
                "initial_goal_source": f"brief:{self.run.id}",
                "initial_goal": self.run.input_goal_text,
                "current_message_ref": f"message:{self.job.id}",
            },
        )


class ProgressiveBudgetGateway:
    def __init__(
        self,
        gateway: ModelGateway,
        transactions: TransactionFactory,
        actor: AuthenticatedActor,
        run_id: UUID,
        job_id: UUID,
    ) -> None:
        self.gateway, self.transactions, self.actor, self.run_id, self.job_id = (
            gateway,
            transactions,
            actor,
            run_id,
            job_id,
        )

    async def generate_structured[T: BaseModel](
        self, request: StructuredModelRequest[T]
    ) -> StructuredModelResponse[T]:
        async with self.transactions(self.actor) as transaction:
            repository = transaction.repository
            await PostgreSQLPlanningDraftRepository(transaction).authorize(self.actor, self.run_id)
            await lock_planning_workflow(
                transaction.session, self.actor.organization_id, self.run_id
            )
            checkpoint = await repository.get_latest_checkpoint(
                actor=self.actor, run_id=self.run_id
            )
            data = dict(checkpoint.state) if checkpoint else {}
            if data.get("execution_id") != str(self.job_id):
                data = {
                    "execution_id": str(self.job_id),
                    "model_calls_used": 0,
                    "input_tokens_reserved": 0,
                    "output_tokens_reserved": 0,
                    "started_at": datetime.now(UTC).isoformat(),
                }
            used = int(data.get("model_calls_used", 0))
            started = datetime.fromisoformat(str(data["started_at"]))
            elapsed = (datetime.now(UTC) - started).total_seconds()
            inputs = (
                sum(len(message.content.encode()) for message in request.messages)
                + len(
                    json.dumps(
                        request.output_schema.model_json_schema(), separators=(",", ":")
                    ).encode()
                )
                + 1024
            )
            outputs = request.max_output_tokens or 2800
            input_used = int(data.get("input_tokens_reserved", 0))
            output_used = int(data.get("output_tokens_reserved", 0))
            if input_used + inputs > 24000 or output_used + outputs > 4000:
                raise ModelInvalidInputError("planning token budget exhausted")
            if used >= 3 or elapsed >= 120:
                raise ModelInvalidInputError("planning execution budget exhausted")
            data["model_calls_used"] = used + 1
            data["input_tokens_reserved"] = input_used + inputs
            data["output_tokens_reserved"] = output_used + outputs
            await repository.save_checkpoint(
                checkpoint=WorkflowCheckpoint(
                    id=uuid4(),
                    organization_id=self.actor.organization_id,
                    workflow_run_id=self.run_id,
                    node="progressive_model_call",
                    sequence=checkpoint.sequence + 1 if checkpoint else 1,
                    state=data,
                )
            )
            await transaction.commit()
        # Reserve the attempt before calling the provider, even if the worker crashes.
        response = await self.gateway.generate_structured(
            replace(
                request,
                timeout_seconds=min(request.timeout_seconds, 120 - elapsed),
                max_output_tokens=outputs,
            )
        )
        if response.usage is not None and (
            response.usage.input_tokens > inputs or response.usage.output_tokens > outputs
        ):
            raise ModelInvalidInputError("provider usage exceeds planning reservation")
        return response


class ProgressivePersistence:
    def __init__(
        self, transactions: TransactionFactory, actor: AuthenticatedActor, job: WorkflowJob
    ) -> None:
        self.transactions, self.actor, self.job = transactions, actor, job
        self.drafts = PlanningDraftService(
            transaction_factory=build_draft_transaction_factory(transactions)
        )

    async def save_checkpoint(self, checkpoint: PlanningCheckpoint) -> None:
        raise RuntimeError("progressive checkpoints use atomic publication")

    async def append_progress(self, event: PlanningProgressEvent) -> None:
        raise RuntimeError("progressive events use atomic publication")

    async def persist_proposal(self, draft: PlanningProposalDraft) -> PersistedProposalReference:
        raise RuntimeError("progressive draft cannot persist a business proposal")

    async def publish_progressive_result(
        self,
        state: ProgressivePlanningState,
        decision: PlanningDecision,
        validation: DraftValidationResult,
    ) -> DraftBinding | None:
        if decision.draft is not None:
            question = decision.question or (
                decision.draft.open_questions[0] if decision.draft.open_questions else None
            )
            snapshot = await self.drafts.publish_version(
                actor=self.actor,
                run_id=state.run_id,
                expected_draft_version=state.current_binding.draft_version
                if state.current_binding
                else None,
                draft=DraftContent(
                    content=decision.draft.model_dump(mode="json"),
                    question=question.model_dump(mode="json") if question else None,
                    validation=asdict(validation),
                    versions={
                        "workflow": state.workflow_version,
                        "prompt": state.prompt_version,
                        "model": state.model_reference,
                        "schema": state.schema_version,
                        "verifier": state.verifier_version,
                        "execution_id": str(self.job.id),
                        "model_calls_used": str(state.model_calls_used),
                        "locale": state.locale,
                    },
                ),
                request_id=f"planning-job:{self.job.id}",
                idempotency_key=f"progressive:{self.job.id}",
            )
            return DraftBinding(
                draft_id=snapshot.draft_id,
                draft_version=snapshot.draft_version,
                fingerprint=snapshot.fingerprint,
            )
        if decision.question is None:
            raise RuntimeError("planning question is missing")
        async with self.transactions(self.actor) as transaction:
            await PostgreSQLPlanningDraftRepository(transaction).authorize(self.actor, state.run_id)
            await lock_planning_workflow(
                transaction.session, self.actor.organization_id, state.run_id
            )
            latest = await transaction.repository.get_latest_checkpoint(
                actor=self.actor, run_id=state.run_id
            )
            if latest and latest.state.get("completed_execution_id") == str(self.job.id):
                return state.current_binding
            run = await transaction.repository.get_workflow_run(
                actor=self.actor, run_id=state.run_id
            )
            if run is None or run.status is not WorkflowRunStatus.RUNNING:
                raise RuntimeError("planning workflow changed")
            data = {
                "workflow_version": "3.0.0",
                "schema_version": "planning-draft.v1",
                "locale": state.locale,
                "question": decision.question.model_dump(mode="json"),
                "pending_questions": [decision.question.text],
                "completed_execution_id": str(self.job.id),
                "model_calls_used": state.model_calls_used,
                "binding": state.current_binding.model_dump(mode="json")
                if state.current_binding
                else None,
            }
            await transaction.repository.save_checkpoint(
                checkpoint=WorkflowCheckpoint(
                    id=uuid4(),
                    organization_id=self.actor.organization_id,
                    workflow_run_id=state.run_id,
                    node="await_manager_input",
                    sequence=latest.sequence + 1 if latest else 1,
                    state=data,
                )
            )
            await transaction.repository.update_workflow_run(
                actor=self.actor, run=run.mark_needs_input()
            )
            await transaction.repository.append_event(
                actor=self.actor,
                run_id=run.id,
                event_type="workflow.needs_input",
                public_payload={
                    "question": decision.question.text,
                    "question_id": decision.question.id,
                    "progressive": True,
                },
            )
            await transaction.commit()
        return state.current_binding


async def execute_progressive_job(
    *,
    settings: Settings,
    retrieval_context: PlanningRetrievalContext | None = None,
    transactions: TransactionFactory,
    actor: AuthenticatedActor,
    run: WorkflowRun,
    job: WorkflowJob,
) -> None:
    from app.modules.planning_runs.adapters.ai_runtime import (
        WorkflowRecordingModelGateway,
        build_model_gateway,
    )

    current_draft, binding, question = None, None, None
    async with transactions(actor) as transaction:
        await PostgreSQLPlanningDraftRepository(transaction).authorize(actor, run.id)
        checkpoint = await transaction.repository.get_latest_checkpoint(actor=actor, run_id=run.id)
        if checkpoint and checkpoint.state.get("completed_execution_id") == str(job.id):
            return
        stored = await transaction.session.scalar(
            select(PlanningDraftModel).where(
                PlanningDraftModel.organization_id == actor.organization_id,
                PlanningDraftModel.workflow_run_id == run.id,
            )
        )
        if stored is not None:
            snapshot = await PostgreSQLPlanningDraftRepository(transaction).get_snapshot(
                actor, stored.id, None
            )
            expected = job.payload.get("expected_draft_version")
            if expected is not None and expected != snapshot.current_version:
                raise ValueError("planning draft version changed before execution")
            current_draft = PlanningDraft.model_validate(snapshot.content.content)
            binding = DraftBinding(
                draft_id=snapshot.draft_id,
                draft_version=snapshot.draft_version,
                fingerprint=snapshot.fingerprint,
            )
        if checkpoint and checkpoint.state.get("question"):
            question = PlanningQuestion.model_validate(checkpoint.state["question"])
        if run.status is WorkflowRunStatus.QUEUED:
            run = await transaction.repository.update_workflow_run(
                actor=actor, run=run.mark_running()
            )
        await transaction.commit()
    old_refs: frozenset[str] = (
        planning_source_references(current_draft) if current_draft else frozenset()
    )
    refs = tuple(sorted(old_refs | {f"brief:{run.id}", f"message:{job.id}"}))
    initial_used = (
        int(checkpoint.state.get("model_calls_used", 0))
        if checkpoint and checkpoint.state.get("execution_id") == str(job.id)
        else 0
    )
    state = ProgressivePlanningState(
        run_id=run.id,
        organization_id=actor.organization_id,
        actor_membership_id=actor.membership_id,
        actor_role=actor.role.value,
        locale=cast(
            PlanningLocale,
            str(
                job.payload.get(
                    "locale", checkpoint.state.get("locale", "en") if checkpoint else "en"
                )
            ),
        ),
        user_brief=str(job.payload.get("manager_message", run.input_goal_text)),
        context_reference_ids=refs,
        current_draft=current_draft,
        current_binding=binding,
        pending_question=question,
        model_calls_used=initial_used,
    )
    gateway = ProgressiveBudgetGateway(
        WorkflowRecordingModelGateway(
            gateway=build_model_gateway(settings),
            transaction_factory=transactions,
            organization_id=run.organization_id,
            workflow_run_id=run.id,
        ),
        transactions,
        actor,
        run.id,
        job.id,
    )
    persistence = ProgressivePersistence(transactions, actor, job)
    graph = PlanningGraph(
        model_gateway=gateway,
        context_port=ProgressiveContext(transactions, actor, run, job, refs, retrieval_context),
        persistence_port=persistence,
        progressive_persistence_port=persistence,
    )
    async with asyncio.timeout(120):
        result = await graph.run_progressive(state)
    if result.safe_error_code is not None:
        async with transactions(actor) as transaction:
            current = await transaction.repository.get_workflow_run(actor=actor, run_id=run.id)
            if current is not None and current.status is WorkflowRunStatus.RUNNING:
                await transaction.repository.update_workflow_run(
                    actor=actor, run=current.mark_failed(result.safe_error_code)
                )
                await transaction.repository.append_event(
                    actor=actor,
                    run_id=run.id,
                    event_type="workflow.failed",
                    public_payload={"safe_error_code": result.safe_error_code},
                )
            await transaction.commit()
