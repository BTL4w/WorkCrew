"""Composition and translation boundary between backend and the AI package."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.planning_runs.application.job_service import JobHandler
from app.modules.planning_runs.application.ports import (
    PlanningRuntimePort,
    PlanningRunTransaction,
)
from app.modules.planning_runs.domain.models import (
    ApprovalStatus,
    ModelInvocation,
    Proposal,
    ProposalStatus,
    ProposalVersion,
    UnsupportedPlanningCapabilityError,
    WorkflowCheckpoint,
    WorkflowEvent,
    WorkflowJob,
    WorkflowRun,
    WorkflowRunStatus,
)
from work_management_ai.model_gateway.contracts import (
    ModelGateway,
    StructuredModelRequest,
    StructuredModelResponse,
)
from work_management_ai.model_gateway.errors import ModelInvalidOutputError, ModelUnavailableError
from work_management_ai.model_gateway.mock import MockModelGateway
from work_management_ai.model_gateway.openai import OpenAIModelGateway
from work_management_ai.schemas.planning import PlanningModelOutput
from work_management_ai.workflows.planning.context import (
    PermittedPlanningContext,
    PlanningContextRequest,
)
from work_management_ai.workflows.planning.graph import PlanningGraph, PlanningGraphResult
from work_management_ai.workflows.planning.policy import evaluate_planning_policy
from work_management_ai.workflows.planning.ports import (
    PersistedProposalReference,
    PlanningCheckpoint,
    PlanningProgressEvent,
    PlanningProposalDraft,
    PlanningRevisionBase,
)
from work_management_ai.workflows.planning.state import (
    PLANNING_SCHEMA_VERSION,
    PLANNING_WORKFLOW_VERSION,
    PlanningLocale,
    create_planning_state,
)
from work_management_ai.workflows.planning.verifier import (
    PLANNING_VERIFIER_VERSION,
    PlanningValidationItem,
    PlanningValidationResult,
    PlanningVerificationContext,
    verify_plan,
)


def _validation_json(validation: PlanningValidationResult) -> dict[str, object]:
    def item_json(finding: PlanningValidationItem) -> dict[str, object]:
        return {
            "path": finding.path,
            "code": finding.code,
            "message_key": finding.message_key,
            "severity": finding.severity,
        }

    return {
        "is_valid": validation.can_approve,
        "can_approve": validation.can_approve,
        "errors": [item_json(item) for item in validation.errors],
        "warnings": [item_json(item) for item in validation.warnings],
    }


class PlanningAIRuntime:
    """Expose deterministic Task 7 runtime metadata and validation to backend."""

    workflow_version = PLANNING_WORKFLOW_VERSION
    verifier_version = PLANNING_VERIFIER_VERSION
    schema_version = PLANNING_SCHEMA_VERSION

    def validate_capability(self, message: str) -> None:
        decision = evaluate_planning_policy(actor_role="MANAGER", user_brief=message)
        if decision.outcome != "ALLOW":
            raise UnsupportedPlanningCapabilityError

    def validate_proposal_content(self, content: dict[str, object]) -> dict[str, object]:
        validated = PlanningModelOutput.model_validate(content)
        return cast(dict[str, object], validated.model_dump(mode="json"))

    def validate_proposal_deterministically(
        self,
        content: dict[str, object],
        *,
        active_membership_ids: frozenset[UUID],
    ) -> dict[str, object]:
        validated = PlanningModelOutput.model_validate(content)
        result = verify_plan(
            validated,
            PlanningVerificationContext(),
        )
        return _validation_json(result)


class _DisabledModelGateway:
    async def generate_structured[StructuredOutputT: BaseModel](
        self, request: StructuredModelRequest[StructuredOutputT]
    ) -> StructuredModelResponse[StructuredOutputT]:
        del request
        raise ModelUnavailableError("model provider is disabled")


def _mock_plan() -> dict[str, object]:
    return {
        "project": {
            "title": "Proposed project",
            "description": "Deterministic Phase 2 acceptance plan",
            "start_date": "2026-09-01",
            "due_date": "2026-09-07",
        },
        "goal": {
            "title": "Proposed goal",
            "description": None,
            "expected_outcomes": ["Manager-reviewed outcome"],
            "target_date": "2026-09-07",
        },
        "milestones": [
            {
                "ref": "m1",
                "title": "Launch readiness",
                "description": "The launch package is reviewed and ready.",
                "due_date": "2026-09-07",
            }
        ],
        "project_weeks": [
            {
                "ref": "w1",
                "week_number": 1,
                "start_date": "2026-09-01",
                "end_date": "2026-09-07",
                "objective": "Prepare and verify the launch package",
            }
        ],
        "tasks": [
            {
                "ref": "t1",
                "project_week_ref": "w1",
                "milestone_ref": "m1",
                "title": "Prepare launch package",
                "description": None,
                "due_date": "2026-09-04",
                "assignee_membership_id": None,
                "required_skill_labels": ["Project Management"],
                "estimated_effort_hours": 8,
                "acceptance_criteria": ["Launch package is ready for review"],
            },
            {
                "ref": "t2",
                "project_week_ref": "w1",
                "milestone_ref": "m1",
                "title": "Verify launch readiness",
                "description": None,
                "due_date": "2026-09-07",
                "assignee_membership_id": None,
                "required_skill_labels": ["Manual Testing"],
                "estimated_effort_hours": 4,
                "acceptance_criteria": ["Readiness review is recorded"],
            },
        ],
        "dependencies": [{"predecessor_ref": "t1", "successor_ref": "t2"}],
        "assumptions": [
            {
                "description": "The launch scope has been approved.",
                "source": "manager_request",
            }
        ],
    }


class _Phase2MockModelGateway:
    """Deterministic fixtures for both Planning Graph and activated Agent contracts."""

    def __init__(self) -> None:
        fixture = _mock_plan()
        self._planning = MockModelGateway(
            fixtures={
                f"planning.{locale}.{mode}": fixture
                for locale in ("vi", "en")
                for mode in ("generate", "repair", "revision")
            }
            | {
                f"planning.{locale}.proposal_revision": {
                    "content": fixture,
                    "change_summary": "AI revision",
                }
                for locale in ("vi", "en")
            }
        )

    async def generate_structured[StructuredOutputT: BaseModel](
        self, request: StructuredModelRequest[StructuredOutputT]
    ) -> StructuredModelResponse[StructuredOutputT]:
        if request.invocation_key.startswith("planning."):
            return await self._planning.generate_structured(request)
        fixture = self._agent_fixture(request)
        try:
            parsed = request.output_schema.model_validate(fixture)
        except (TypeError, ValidationError, ValueError) as error:
            raise ModelInvalidOutputError("mock fixture failed schema validation") from error
        return StructuredModelResponse(parsed=parsed, model_ref="mock:phase2-assistant-v1")

    @staticmethod
    def _payload[StructuredOutputT: BaseModel](
        request: StructuredModelRequest[StructuredOutputT],
    ) -> dict[str, object]:
        if not request.messages:
            return {}
        try:
            value = json.loads(request.messages[-1].content)
        except (json.JSONDecodeError, TypeError):
            return {}
        return cast(dict[str, object], value) if isinstance(value, dict) else {}

    def _agent_fixture[StructuredOutputT: BaseModel](
        self, request: StructuredModelRequest[StructuredOutputT]
    ) -> dict[str, object]:
        key = request.invocation_key
        payload = self._payload(request)
        locale = "vi" if ".vi." in key else "en"
        if key.startswith("daily_update.") and key.endswith(".extract"):
            # Local fixture only; hosted interpretation always goes through the gateway.
            import re

            text = str(payload.get("report", ""))
            percentages = re.findall(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*%", text)
            return {
                "reported_percent": percentages[0] if len(percentages) == 1 else None,
                "remaining_hours": None,
                "spent_hours": None,
                "done_text": text,
                "next_steps": "",
                "needs_clarification": len(percentages) != 1,
            }
        if key == "daily_update.claims":
            raw = payload.get("report_lines") or json.loads(request.messages[-1].content)
            if not isinstance(raw, list):
                raise ModelInvalidOutputError("mock claim inventory input invalid")
            lines = cast(list[dict[str, object]], raw)
            return {
                "claims": [
                    {
                        "source_claim_id": line["id"],
                        "text": line["text"],
                        "category": "WORK",
                        "checkability": True,
                    }
                    for line in lines
                ]
            }
        if key == "daily_update.compare":
            claims = cast(list[dict[str, object]], payload.get("claims", []))
            return {
                "findings": [
                    {
                        "claim_id": claim["id"],
                        "finding": "UNASSESSABLE",
                        "source_refs": [],
                        "limitation": "Mock provider cannot verify original images.",
                    }
                    for claim in claims
                ]
            }
        if key.startswith("orchestrator.") and key.endswith(".synthesize"):
            return {
                "blocks": [
                    {
                        "kind": "work_evidence",
                        "summary": (
                            "Chỉ hiển thị dữ liệu công việc mà bạn được phép xem."
                            if locale == "vi"
                            else "Only work data you are permitted to view is shown."
                        ),
                        "evidence": cast(list[object], []),
                    }
                ]
            }
        if key.startswith("orchestrator.") and any(
            marker in key for marker in (".plan", ".repair", ".replan.")
        ):
            return self._orchestrator_plan(payload, locale)
        if key.startswith("work_intelligence.") and key.endswith(".plan"):
            return {
                "question_kind": "MY_TASKS",
                "skill_reference": "answer_work_question@1",
                "tool_id": "work.read_my_tasks",
                "tool_input": {
                    "status": None,
                    "due_from": None,
                    "due_to": None,
                    "limit": 20,
                },
                "requested_handoff": None,
            }
        if key.startswith("work_intelligence.") and key.endswith(".synthesize"):
            return {
                "question_kind": "MY_TASKS",
                "claims": [],
                "needs_clarification": False,
                "clarification_question": None,
            }
        if key.startswith("planning_agent.") and key.endswith(".step_plan"):
            return {
                "skill_reference": "create_project_plan@1",
                "tool_id": "planning.manage_run",
                "tool_input": {},
                "requested_handoff": None,
            }
        raise ModelUnavailableError("model fixture unavailable")

    @staticmethod
    def _orchestrator_plan(payload: dict[str, object], locale: str) -> dict[str, object]:
        message = str(payload.get("message", "")).casefold()
        raw_catalog = payload.get("specialist_catalog", [])
        catalog = cast(list[object], raw_catalog) if isinstance(raw_catalog, list) else []

        def is_planning_entry(item: object) -> bool:
            if not isinstance(item, dict):
                return False
            entry = cast(dict[str, object], item)
            capabilities = entry.get("capabilities")
            return (
                entry.get("agent_id") == "planning"
                and isinstance(capabilities, list)
                and "planning.create" in cast(list[object], capabilities)
            )

        planning_available = any(is_planning_entry(item) for item in catalog)

        def is_assignment_entry(item: object) -> bool:
            if not isinstance(item, dict):
                return False
            entry = cast(dict[str, object], item)
            capabilities = entry.get("capabilities")
            return (
                entry.get("agent_id") == "assignment"
                and isinstance(capabilities, list)
                and "assignment.recommend_team" in cast(list[object], capabilities)
            )

        assignment_available = any(is_assignment_entry(item) for item in catalog)
        asks_for_team = any(signal in message for signal in ("project", "dự án")) and any(
            signal in message for signal in ("team", "đội ngũ", "nhóm", "nhân sự")
        )
        asks_for_planning = any(
            signal in message
            for signal in (
                "plan",
                "planning",
                "proposal",
                "project plan",
                "kế hoạch",
                "lập kế hoạch",
                "dự án",
            )
        )
        if asks_for_planning and not planning_available:
            return {
                "objectives": [message or "planning request"],
                "steps": [],
                "unavailable_capabilities": ["planning.create"],
                "response_language": locale,
            }
        if asks_for_planning:
            steps: list[dict[str, object]] = [
                {
                    "step_id": "create_plan",
                    "target_agent_id": "planning",
                    "target_agent_version": "1.0.0",
                    "capability": "planning.create",
                    "objective": message or "create a Project plan",
                    "typed_input": {},
                    "depends_on": [],
                    "mode": "PROPOSAL",
                }
            ]
            if asks_for_team and assignment_available:
                steps.append(
                    {
                        "step_id": "recommend_team",
                        "target_agent_id": "assignment",
                        "target_agent_version": "1.0.0",
                        "capability": "assignment.recommend_team",
                        "objective": "Recommend a Project Team after Project approval",
                        "typed_input": {},
                        "depends_on": ["create_plan"],
                        "mode": "PROPOSAL",
                    }
                )
            return {
                "objectives": [message or "create a Project plan"],
                "steps": steps,
                "unavailable_capabilities": [],
                "response_language": locale,
            }
        return {
            "objectives": [message or "read permitted work"],
            "steps": [
                {
                    "step_id": "read_my_tasks",
                    "target_agent_id": "work_intelligence",
                    "target_agent_version": "1.0.0",
                    "capability": "work.read_my_tasks",
                    "objective": message or "read permitted work",
                    "typed_input": {
                        "question": message or "What are my current tasks?",
                        "locale": locale,
                        "requested_kind": "MY_TASKS",
                        "entity_reference": None,
                    },
                    "depends_on": [],
                    "mode": "READ_ONLY",
                }
            ],
            "unavailable_capabilities": [],
            "response_language": locale,
        }


def build_model_gateway(settings: Settings) -> ModelGateway:
    if settings.ai_provider == "openai" and settings.openai_api_key is not None:
        return OpenAIModelGateway(
            model_name=settings.ai_model,
            api_key=settings.openai_api_key,
        )
    if settings.ai_provider == "mock":
        return _Phase2MockModelGateway()
    return _DisabledModelGateway()


class CurrentActorResolver(Protocol):
    async def resolve(
        self, *, organization_id: UUID, membership_id: UUID
    ) -> AuthenticatedActor | None: ...


type PlanningTransactionFactory = Callable[[AuthenticatedActor | UUID], PlanningRunTransaction]


class WorkflowRecordingModelGateway:
    """Record allowlisted invocation metadata after a transaction-free model call."""

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        transaction_factory: PlanningTransactionFactory,
        organization_id: UUID,
        workflow_run_id: UUID,
    ) -> None:
        self._gateway = gateway
        self._transactions = transaction_factory
        self._organization_id = organization_id
        self._run_id = workflow_run_id

    async def generate_structured[StructuredOutputT: BaseModel](
        self, request: StructuredModelRequest[StructuredOutputT]
    ) -> StructuredModelResponse[StructuredOutputT]:
        started = monotonic()
        status = "SUCCESS"
        model_ref = "unavailable"
        try:
            response = await self._gateway.generate_structured(request)
            model_ref = response.model_ref
        except Exception:
            status = "FAILED"
            model_ref = "unavailable"
            raise
        finally:
            provider, _, model_name = model_ref.partition(":")
            async with self._transactions(self._organization_id) as transaction:
                await transaction.repository.record_model_invocation(
                    invocation=ModelInvocation(
                        id=uuid4(),
                        organization_id=self._organization_id,
                        workflow_run_id=self._run_id,
                        provider=provider or "unknown",
                        model_name=model_name or model_ref,
                        prompt_version=request.invocation_key,
                        schema_version=PLANNING_SCHEMA_VERSION,
                        invocation_key=request.invocation_key,
                        duration_ms=max(0, int((monotonic() - started) * 1000)),
                        status=status,
                    )
                )
                await transaction.commit()
        return response


class _PlanningContextAdapter:
    def __init__(self, transaction_factory: PlanningTransactionFactory, run: WorkflowRun) -> None:
        self._transactions = transaction_factory
        self._run = run

    async def load_permitted_context(
        self, request: PlanningContextRequest
    ) -> PermittedPlanningContext:
        if (
            request.organization_id != self._run.organization_id
            or request.run_id != self._run.id
            or request.actor_membership_id != self._run.requested_by_membership_id
        ):
            raise PermissionError("planning context scope mismatch")
        questions: tuple[str, ...] = ()
        if len(request.user_brief.split()) < 4 and not request.manager_answers:
            questions = ("Please provide more planning detail.",)
        return PermittedPlanningContext(
            reference_ids=(),
            required_questions=questions,
            structured_facts={},
        )


class _PlanningPersistenceAdapter:
    def __init__(
        self, transaction_factory: PlanningTransactionFactory, actor: AuthenticatedActor
    ) -> None:
        self._transactions = transaction_factory
        self._actor = actor

    async def save_checkpoint(self, checkpoint: PlanningCheckpoint) -> None:
        async with self._transactions(self._actor) as transaction:
            latest = await transaction.repository.get_latest_checkpoint(
                actor=self._actor, run_id=checkpoint.run_id
            )
            state = {**checkpoint.state, "_persistence_key": checkpoint.idempotency_key}
            if latest is None or latest.state.get("_persistence_key") != checkpoint.idempotency_key:
                await transaction.repository.save_checkpoint(
                    checkpoint=WorkflowCheckpoint(
                        id=uuid4(),
                        organization_id=checkpoint.organization_id,
                        workflow_run_id=checkpoint.run_id,
                        node=checkpoint.node,
                        sequence=1 if latest is None else latest.sequence + 1,
                        state=state,
                    )
                )
            await transaction.commit()

    async def append_progress(self, event: PlanningProgressEvent) -> None:
        payload = {**event.public_payload, "stage": event.stage}
        async with self._transactions(self._actor) as transaction:
            existing = await transaction.repository.list_events(
                actor=self._actor, run_id=event.run_id
            )
            event_type = f"workflow.{event.stage.casefold()}"
            if not (
                existing
                and existing[-1].event_type == event_type
                and existing[-1].public_payload == payload
            ):
                await transaction.repository.append_event(
                    event=WorkflowEvent(
                        id=uuid4(),
                        organization_id=event.organization_id,
                        workflow_run_id=event.run_id,
                        sequence=0,
                        event_type=event_type,
                        public_payload=payload,
                    )
                )
            await transaction.commit()

    async def persist_proposal(self, draft: PlanningProposalDraft) -> PersistedProposalReference:
        async with self._transactions(self._actor) as transaction:
            existing = await transaction.repository.get_proposal_by_run_id(
                actor=self._actor, run_id=draft.run_id
            )
            if existing is not None:
                await transaction.commit()
                return PersistedProposalReference(
                    proposal_id=existing.id,
                    version=existing.current_version_number,
                )
            proposal = Proposal.create(
                organization_id=draft.organization_id,
                workflow_run_id=draft.run_id,
            )
            validation = _validation_json(draft.validation)
            version = ProposalVersion(
                id=uuid4(),
                organization_id=draft.organization_id,
                proposal_id=proposal.id,
                version_number=1,
                created_by_membership_id=draft.actor_membership_id,
                content=cast(dict[str, object], draft.content.model_dump(mode="json")),
                assumptions=[item.model_dump(mode="json") for item in draft.content.assumptions],
                field_provenance={"default": "AI_PROPOSED"},
                validation_result=validation,
                source_reference_snapshot=[
                    {"reference_id": value} for value in draft.context_reference_ids
                ],
                workflow_version=draft.workflow_version,
                prompt_version=draft.prompt_version,
                schema_version=draft.schema_version,
                model_reference=draft.model_reference,
                verifier_version=draft.verifier_version,
                creator_type="AI_SYSTEM",
            )
            await transaction.repository.create_proposal(proposal=proposal, initial_version=version)
            ready_proposal = await transaction.repository.complete_proposal_revalidation(
                actor=self._actor,
                proposal_id=proposal.id,
                version_number=1,
                validation_result=validation,
                request_id=f"workflow-run:{draft.run_id}",
            )
            await transaction.repository.append_event(
                event=WorkflowEvent(
                    id=uuid4(),
                    organization_id=draft.organization_id,
                    workflow_run_id=draft.run_id,
                    sequence=0,
                    event_type=(
                        "proposal.ready"
                        if ready_proposal.status is ProposalStatus.READY_FOR_DECISION
                        else "proposal.validation_failed"
                    ),
                    public_payload={
                        "proposal_id": str(ready_proposal.id),
                        "version": ready_proposal.current_version_number,
                        "approval_id": (
                            str(ready_proposal.approval_id)
                            if ready_proposal.approval_id is not None
                            else None
                        ),
                        "can_approve": draft.validation.can_approve,
                        "error_codes": [item.code for item in draft.validation.errors],
                    },
                )
            )
            await transaction.commit()
            return PersistedProposalReference(
                proposal_id=ready_proposal.id,
                version=ready_proposal.current_version_number,
            )


class _RevisionOnlyPersistenceAdapter:
    """Fail closed if the revision-only graph tries any persistence callback."""

    async def save_checkpoint(self, checkpoint: PlanningCheckpoint) -> None:
        del checkpoint
        raise RuntimeError("AI_REVISION_PERSISTENCE_FORBIDDEN")

    async def append_progress(self, event: PlanningProgressEvent) -> None:
        del event
        raise RuntimeError("AI_REVISION_PERSISTENCE_FORBIDDEN")

    async def persist_proposal(self, draft: PlanningProposalDraft) -> PersistedProposalReference:
        del draft
        raise RuntimeError("AI_REVISION_PERSISTENCE_FORBIDDEN")


class PlanningJobHandler:
    """Execute only bounded Task 7 start/resume instructions."""

    def __init__(
        self,
        settings: Settings,
        transaction_factory: PlanningTransactionFactory,
        actor_resolver: CurrentActorResolver,
    ) -> None:
        self._settings = settings
        self._transactions = transaction_factory
        self._actors = actor_resolver

    async def __call__(self, *, job: WorkflowJob, worker_id: str) -> None:
        del worker_id
        async with self._transactions(job.organization_id) as transaction:
            run = await transaction.repository.get_workflow_run_by_scope(
                organization_id=job.organization_id,
                run_id=job.workflow_run_id,
            )
            await transaction.commit()
        if run is None:
            raise RuntimeError("WORKFLOW_RUN_UNAVAILABLE")
        actor = await self._actors.resolve(
            organization_id=run.organization_id,
            membership_id=run.requested_by_membership_id,
        )
        if actor is None or actor.role not in {MembershipRole.ADMIN, MembershipRole.MANAGER}:
            raise RuntimeError("ACTOR_CONTEXT_UNAVAILABLE")
        gateway = WorkflowRecordingModelGateway(
            gateway=build_model_gateway(self._settings),
            transaction_factory=self._transactions,
            organization_id=run.organization_id,
            workflow_run_id=run.id,
        )
        graph = PlanningGraph(
            model_gateway=gateway,
            context_port=_PlanningContextAdapter(self._transactions, run),
            persistence_port=_PlanningPersistenceAdapter(self._transactions, actor),
        )
        result: PlanningGraphResult
        if job.job_type == "planning.start":
            if run.status is WorkflowRunStatus.QUEUED:
                async with self._transactions(actor) as transaction:
                    run = await transaction.repository.update_workflow_run(
                        actor=actor, run=run.mark_running()
                    )
                    await transaction.commit()
            result = await graph.run(
                create_planning_state(
                    run_id=run.id,
                    organization_id=run.organization_id,
                    actor_membership_id=run.requested_by_membership_id,
                    actor_role=actor.role.value,
                    locale=cast(PlanningLocale, str(job.payload.get("locale", "en"))),
                    user_brief=run.input_goal_text,
                )
            )
        elif job.job_type == "planning.resume":
            async with self._transactions(actor) as transaction:
                checkpoint = await transaction.repository.get_latest_checkpoint(
                    actor=actor, run_id=run.id
                )
                await transaction.commit()
            if checkpoint is None or checkpoint.node != "await_manager_input":
                raise RuntimeError("manager-input checkpoint is unavailable")
            result = await graph.resume_from_checkpoint(
                checkpoint.state,
                str(job.payload.get("manager_message", "")),
            )
        else:
            raise RuntimeError("unsupported planning job type")
        await self._apply_result(actor, run, result)

    async def _apply_result(
        self,
        actor: AuthenticatedActor,
        run: WorkflowRun,
        result: PlanningGraphResult,
    ) -> None:
        failure_code: str | None = None
        if result.interrupt is not None and result.interrupt.kind == "MANAGER_INPUT_REQUIRED":
            updated = run.mark_needs_input()
        elif result.interrupt is not None and result.interrupt.kind == "MANAGER_DECISION_REQUIRED":
            updated = run.mark_waiting_for_decision()
        elif result.state["stage"] in {"MANUAL_FALLBACK", "UNSUPPORTED", "FORBIDDEN"}:
            failure_code = "AI_WORKFLOW_UNAVAILABLE"
            updated = run.mark_failed(failure_code)
        else:
            return
        async with self._transactions(actor) as transaction:
            await transaction.repository.update_workflow_run(actor=actor, run=updated)
            if failure_code is not None:
                await transaction.repository.append_event(
                    actor=actor,
                    run_id=run.id,
                    event_type="workflow.failed",
                    public_payload={
                        "safe_error_code": failure_code,
                        "stage": result.state["stage"],
                    },
                )
            await transaction.commit()


class ProposalRevalidationJobHandler:
    """Deterministically validate the edited immutable version without applying it."""

    def __init__(
        self,
        transaction_factory: PlanningTransactionFactory,
        actor_resolver: CurrentActorResolver,
    ) -> None:
        self._transactions = transaction_factory
        self._actors = actor_resolver

    async def __call__(self, *, job: WorkflowJob, worker_id: str) -> None:
        del worker_id
        proposal_id = UUID(str(job.payload.get("proposal_id", "")))
        version_number = int(job.payload.get("proposal_version", 0))
        async with self._transactions(job.organization_id) as transaction:
            run = await transaction.repository.get_workflow_run_by_scope(
                organization_id=job.organization_id,
                run_id=job.workflow_run_id,
            )
            creator_id = await transaction.repository.get_proposal_version_creator_by_scope(
                organization_id=job.organization_id,
                proposal_id=proposal_id,
                version_number=version_number,
            )
            await transaction.commit()
        if run is None:
            raise RuntimeError("WORKFLOW_RUN_UNAVAILABLE")
        if creator_id is None:
            raise RuntimeError("PROPOSAL_VERSION_UNAVAILABLE")
        actor = await self._actors.resolve(
            organization_id=job.organization_id, membership_id=creator_id
        )
        if actor is None or actor.role not in {MembershipRole.ADMIN, MembershipRole.MANAGER}:
            raise RuntimeError("ACTOR_CONTEXT_UNAVAILABLE")
        async with self._transactions(actor) as transaction:
            version = await transaction.repository.get_proposal_version(
                actor=actor,
                proposal_id=proposal_id,
                version_number=version_number,
            )
            if version is None:
                raise RuntimeError("PROPOSAL_VERSION_UNAVAILABLE")
            validation = verify_plan(
                PlanningModelOutput.model_validate(version.content),
                PlanningVerificationContext(),
            )
            public_validation = _validation_json(validation)
            await transaction.repository.append_event(
                event=WorkflowEvent(
                    id=uuid4(),
                    organization_id=job.organization_id,
                    workflow_run_id=job.workflow_run_id,
                    sequence=0,
                    event_type="proposal.validating",
                    public_payload={
                        "proposal_id": str(job.payload.get("proposal_id", "")),
                        "version": int(job.payload.get("proposal_version", 0)),
                    },
                    created_at=datetime.now(UTC),
                )
            )
            proposal = await transaction.repository.complete_proposal_revalidation(
                actor=actor,
                proposal_id=proposal_id,
                version_number=version_number,
                validation_result=public_validation,
                request_id=f"workflow-job:{job.id}",
            )
            await transaction.repository.append_event(
                event=WorkflowEvent(
                    id=uuid4(),
                    organization_id=job.organization_id,
                    workflow_run_id=job.workflow_run_id,
                    sequence=0,
                    event_type=(
                        "proposal.ready"
                        if proposal.status.value == "READY_FOR_DECISION"
                        else "proposal.validation_failed"
                    ),
                    public_payload={
                        "proposal_id": str(proposal_id),
                        "version": version_number,
                        "approval_id": (
                            str(proposal.approval_id) if proposal.approval_id is not None else None
                        ),
                        "can_approve": validation.can_approve,
                        "error_codes": [item.code for item in validation.errors],
                    },
                )
            )
            await transaction.commit()


class ProposalAIRevisionJobHandler:
    """Generate outside a transaction, then append once behind locked predicates."""

    def __init__(
        self,
        *,
        transaction_factory: PlanningTransactionFactory,
        actor_resolver: CurrentActorResolver,
        graph_factory: Callable[[WorkflowRun], PlanningGraph],
        runtime: PlanningRuntimePort,
    ) -> None:
        self._transactions = transaction_factory
        self._actors = actor_resolver
        self._graphs = graph_factory
        self._runtime = runtime

    async def __call__(self, *, job: WorkflowJob, worker_id: str) -> None:
        del worker_id
        try:
            proposal_id = UUID(str(job.payload["proposal_id"]))
            base_version = int(job.payload["base_version"])
            requester_id = UUID(str(job.payload["requester_membership_id"]))
            instruction = str(job.payload["instruction"])
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("AI_REVISION_PAYLOAD_INVALID") from error
        actor = await self._actors.resolve(
            organization_id=job.organization_id,
            membership_id=requester_id,
        )
        if actor is None or actor.role not in {MembershipRole.ADMIN, MembershipRole.MANAGER}:
            raise RuntimeError("ACTOR_CONTEXT_UNAVAILABLE")
        async with self._transactions(actor) as transaction:
            preflight = await transaction.repository.get_ai_revision_preflight(
                actor=actor,
                proposal_id=proposal_id,
                base_version=base_version,
            )
            await transaction.commit()
        if preflight is None or preflight.run.id != job.workflow_run_id:
            return
        try:
            context = PermittedPlanningContext(
                reference_ids=tuple(
                    str(item.get("reference_id", ""))
                    for item in preflight.version.source_reference_snapshot
                ),
                required_questions=(),
                structured_facts={"proposal_version": base_version},
            )
            revision = await self._graphs(preflight.run).generate_revision(
                base=PlanningRevisionBase(
                    proposal_id=proposal_id,
                    version=base_version,
                    locale=cast(PlanningLocale, preflight.locale),
                    content=PlanningModelOutput.model_validate(preflight.version.content),
                ),
                instruction=instruction,
                context=context,
            )
        except Exception:
            if job.attempt_count >= job.max_attempts:
                async with self._transactions(actor) as transaction:
                    await transaction.repository.append_event(
                        actor=actor,
                        run_id=job.workflow_run_id,
                        event_type="proposal.revision_failed",
                        public_payload={
                            "proposal_id": str(proposal_id),
                            "base_version": base_version,
                            "safe_error_code": "AI_REVISION_MODEL_FAILED",
                            "manual_fallback": "PROJECT_TASK_EDITOR",
                        },
                    )
                    await transaction.commit()
            raise RuntimeError("AI_REVISION_MODEL_FAILED") from None

        current_actor = await self._actors.resolve(
            organization_id=job.organization_id,
            membership_id=requester_id,
        )
        if current_actor is None or current_actor.role not in {
            MembershipRole.ADMIN,
            MembershipRole.MANAGER,
        }:
            raise RuntimeError("ACTOR_CONTEXT_UNAVAILABLE")
        content = cast(dict[str, object], revision.content.model_dump(mode="json"))
        async with self._transactions(current_actor) as transaction:
            validation = self._runtime.validate_proposal_deterministically(
                content,
                active_membership_ids=frozenset(),
            )
            appended = await transaction.repository.finalize_ai_revision_mutation(
                actor=current_actor,
                proposal_id=proposal_id,
                base_version=base_version,
                content=content,
                change_summary=revision.change_summary,
                model_reference=revision.model_reference,
                validation_result=validation,
                request_id=f"workflow-job:{job.id}",
                idempotency_key=f"proposal-ai-revise:{job.id}",
            )
            if appended is None:
                await transaction.repository.append_event(
                    actor=current_actor,
                    run_id=job.workflow_run_id,
                    event_type="proposal.revision_stale",
                    public_payload={
                        "proposal_id": str(proposal_id),
                        "base_version": base_version,
                        "safe_error_code": "PROPOSAL_REVISION_STALE",
                    },
                )
            await transaction.commit()


class PlanningFinalizationJobHandler:
    """Finalize a committed Manager decision without invoking the model."""

    def __init__(
        self,
        transaction_factory: PlanningTransactionFactory,
        actor_resolver: CurrentActorResolver,
    ) -> None:
        self._transactions = transaction_factory
        self._actors = actor_resolver

    async def __call__(self, *, job: WorkflowJob, worker_id: str) -> None:
        del worker_id
        try:
            approval_id = UUID(str(job.payload["approval_id"]))
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("FINALIZATION_PAYLOAD_INVALID") from error
        async with self._transactions(job.organization_id) as scope_transaction:
            run = await scope_transaction.repository.get_workflow_run_by_scope(
                organization_id=job.organization_id,
                run_id=job.workflow_run_id,
            )
            decider_id = await scope_transaction.repository.get_approval_decider_by_scope(
                organization_id=job.organization_id, approval_id=approval_id
            )
            await scope_transaction.commit()
        if run is None:
            raise RuntimeError("WORKFLOW_RUN_UNAVAILABLE")
        if run.status is WorkflowRunStatus.COMPLETED:
            return
        if run.status is not WorkflowRunStatus.WAITING_FOR_DECISION:
            raise RuntimeError("WORKFLOW_RUN_NOT_WAITING_FOR_DECISION")
        if decider_id is None:
            raise RuntimeError("APPROVAL_DECIDER_UNAVAILABLE")
        actor = await self._actors.resolve(
            organization_id=job.organization_id, membership_id=decider_id
        )
        if actor is None:
            raise RuntimeError("ACTOR_CONTEXT_UNAVAILABLE")
        try:
            proposal_id = UUID(str(job.payload["proposal_id"]))
            proposal_version = int(job.payload["proposal_version"])
            checkpoint_sequence = int(job.payload["checkpoint_sequence"])
            decision = str(job.payload["decision"])
            project_id = (
                UUID(str(job.payload["project_id"]))
                if job.payload.get("project_id") is not None
                else None
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("FINALIZATION_PAYLOAD_INVALID") from error
        async with self._transactions(actor) as transaction:
            approval = await transaction.repository.get_approval(
                actor=actor,
                approval_id=approval_id,
            )
            proposal = await transaction.repository.get_proposal(
                actor=actor,
                proposal_id=proposal_id,
            )
            checkpoint = await transaction.repository.get_latest_checkpoint(
                actor=actor,
                run_id=run.id,
            )
            expected = {
                "APPROVE": (ApprovalStatus.APPROVED, ProposalStatus.APPROVED),
                "REJECT": (ApprovalStatus.REJECTED, ProposalStatus.REJECTED),
            }.get(decision)
            if (
                approval is None
                or proposal is None
                or expected is None
                or approval.status is not expected[0]
                or proposal.status is not expected[1]
                or approval.proposal_id != proposal.id
                or approval.proposal_version_number != proposal_version
                or proposal.current_version_number != proposal_version
                or proposal.workflow_run_id != run.id
                or checkpoint is None
                or checkpoint.node != "await_manager_decision"
                or checkpoint.sequence != checkpoint_sequence
            ):
                raise RuntimeError("COMMITTED_DECISION_MISMATCH")
            completed = run.mark_completed()
            await transaction.repository.update_workflow_run(actor=actor, run=completed)
            await transaction.repository.save_checkpoint(
                checkpoint=WorkflowCheckpoint(
                    id=uuid4(),
                    organization_id=run.organization_id,
                    workflow_run_id=run.id,
                    node="completed",
                    sequence=checkpoint.sequence + 1,
                    state={
                        "stage": "COMPLETED",
                        "decision": decision,
                        "proposal_id": str(proposal.id),
                        "proposal_version": proposal_version,
                        "approval_id": str(approval.id),
                        "project_id": str(project_id) if project_id else None,
                    },
                )
            )
            await transaction.repository.append_event(
                event=WorkflowEvent(
                    id=uuid4(),
                    organization_id=run.organization_id,
                    workflow_run_id=run.id,
                    sequence=0,
                    event_type="workflow.completed",
                    public_payload={
                        "status": "COMPLETED",
                        "decision": decision,
                        "proposal_id": str(proposal.id),
                        "proposal_version": proposal_version,
                        "approval_id": str(approval.id),
                        "project_id": str(project_id) if project_id else None,
                    },
                )
            )
            await transaction.commit()


def build_planning_job_handlers(
    settings: Settings,
    transaction_factory: PlanningTransactionFactory,
    actor_resolver: CurrentActorResolver,
) -> dict[str, JobHandler]:
    planning = PlanningJobHandler(settings, transaction_factory, actor_resolver)
    runtime = PlanningAIRuntime()

    def revision_graph(run: WorkflowRun) -> PlanningGraph:
        return PlanningGraph(
            model_gateway=WorkflowRecordingModelGateway(
                gateway=build_model_gateway(settings),
                transaction_factory=transaction_factory,
                organization_id=run.organization_id,
                workflow_run_id=run.id,
            ),
            context_port=_PlanningContextAdapter(transaction_factory, run),
            persistence_port=_RevisionOnlyPersistenceAdapter(),
        )

    return {
        "planning.start": planning,
        "planning.resume": planning,
        "proposal.ai_revise": ProposalAIRevisionJobHandler(
            transaction_factory=transaction_factory,
            actor_resolver=actor_resolver,
            graph_factory=revision_graph,
            runtime=runtime,
        ),
        "proposal.revalidate": ProposalRevalidationJobHandler(transaction_factory, actor_resolver),
        "planning.finalize": PlanningFinalizationJobHandler(transaction_factory, actor_resolver),
    }
