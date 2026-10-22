"""SQL adapters for immutable source bindings, paid receipts and current owner scope."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.assistant.adapters.database_models import (
    AgentRunModel,
    AssistantConversationModel,
    AssistantJobModel,
    AssistantMessageModel,
    AssistantTurnModel,
    OrchestrationRunModel,
    ToolInvocationModel,
)
from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.audit.domain.events import AuditOutcome
from app.modules.feedback.adapters.payload_repository import assert_context_live
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.adapters.database_models import MembershipModel
from app.modules.retrieval.adapters.database_models import (
    RetrievalAttemptModel,
    RetrievalRunBudgetModel,
    RetrievalSourceSnapshotModel,
)
from app.modules.retrieval.domain.models import (
    RetrievalPolicyError,
    RetrievalReservation,
    RetrievedSource,
)
from app.modules.retrieval.domain.public_urls import validate_public_url
from app.modules.retrieval.domain.state import (
    PARENT_KEYS,
    RetrievalAttempt,
    RetrievalBudgetState,
    RetrievalLease,
    RetrievalRunScope,
    RetrievalSnapshot,
)


def _budget(row: RetrievalRunBudgetModel) -> RetrievalBudgetState:
    return RetrievalBudgetState(
        row.organization_id,
        row.agent_run_id,
        row.owner_membership_id,
        row.orchestration_run_id,
        RetrievalLease(row.job_id, row.worker_id, row.job_attempt),
        row.fencing_token,
        row.scope,
        row.caps,
        row.reserved,
        row.started_at,
        row.deadline,
        row.expires_at,
        row.version,
    )


def _snapshot(row: RetrievalSourceSnapshotModel) -> RetrievalSnapshot:
    data = row.payload
    source = RetrievedSource(
        validate_public_url(row.canonical_url),
        data["title"],
        data["text"],
        data["method"],
        row.fingerprint,
        row.observed_at,
        datetime.fromisoformat(data["published_at"]) if data["published_at"] else None,
        datetime.fromisoformat(data["source_fetched_at"]) if data["source_fetched_at"] else None,
        data["provider_version"],
    )
    return RetrievalSnapshot(
        row.id,
        row.organization_id,
        row.owner_membership_id,
        row.agent_run_id,
        row.tool_invocation_id,
        source,
        row.expires_at,
    )


class PostgreSQLRetrievalRepository:
    def __init__(self, session: AsyncSession, actor: AuthenticatedActor) -> None:
        self.session, self.actor = session, actor

    async def bind(
        self, run_id: UUID, lease: RetrievalLease | None, *, allow_terminal: bool = False
    ) -> RetrievalRunScope:
        active = await self.session.scalar(
            text("SELECT public.lock_active_membership(:org,:member)"),
            {"org": self.actor.organization_id, "member": self.actor.membership_id},
        )
        member = await self.session.scalar(
            select(MembershipModel).where(
                MembershipModel.organization_id == self.actor.organization_id,
                MembershipModel.id == self.actor.membership_id,
            )
        )
        if active is not True or member is None or member.user_id != self.actor.user_id:
            raise RetrievalPolicyError("RETRIEVAL_ACTOR_UNAVAILABLE")
        now = cast(datetime, await self.session.scalar(text("SELECT clock_timestamp()")))
        lease_until = None
        job = None
        if lease is not None:
            job = await self.session.scalar(
                select(AssistantJobModel)
                .where(
                    AssistantJobModel.organization_id == self.actor.organization_id,
                    AssistantJobModel.id == lease.job_id,
                )
                .with_for_update()
            )
            if (
                job is None
                or job.status != "RUNNING"
                or job.requester_membership_id != self.actor.membership_id
                or job.locked_by != lease.worker_id
                or job.attempt_count != lease.attempt_no
                or job.lease_until is None
                or job.lease_until <= now
            ):
                raise RetrievalPolicyError("RETRIEVAL_JOB_CLAIM_LOST")
            lease_until = job.lease_until
        parent = await self.session.scalar(
            select(OrchestrationRunModel)
            .join(
                AgentRunModel,
                (AgentRunModel.organization_id == OrchestrationRunModel.organization_id)
                & (AgentRunModel.orchestration_run_id == OrchestrationRunModel.id),
            )
            .where(
                AgentRunModel.organization_id == self.actor.organization_id,
                AgentRunModel.id == run_id,
            )
            .with_for_update(of=OrchestrationRunModel)
        )
        agent = await self.session.scalar(
            select(AgentRunModel)
            .where(
                AgentRunModel.organization_id == self.actor.organization_id,
                AgentRunModel.id == run_id,
            )
            .with_for_update()
        )
        if (
            parent is None
            or agent is None
            or parent.actor_membership_id != self.actor.membership_id
            or parent.trigger_kind != "CHAT_TURN"
            or parent.turn_id is None
        ):
            raise RetrievalPolicyError("RETRIEVAL_RUN_UNAVAILABLE")
        if lease is not None and job is not None and job.orchestration_run_id != parent.id:
            raise RetrievalPolicyError("RETRIEVAL_JOB_CLAIM_LOST")
        turn = await self.session.scalar(
            select(AssistantTurnModel).where(
                AssistantTurnModel.organization_id == self.actor.organization_id,
                AssistantTurnModel.id == parent.turn_id,
            )
        )
        conversation = (
            await self.session.scalar(
                select(AssistantConversationModel).where(
                    AssistantConversationModel.organization_id == self.actor.organization_id,
                    AssistantConversationModel.id == turn.conversation_id,
                )
            )
            if turn
            else None
        )
        message = (
            await self.session.scalar(
                select(AssistantMessageModel).where(
                    AssistantMessageModel.organization_id == self.actor.organization_id,
                    AssistantMessageModel.id == turn.user_message_id,
                )
            )
            if turn
            else None
        )
        if (
            conversation is None
            or conversation.owner_membership_id != self.actor.membership_id
            or conversation.status != "ACTIVE"
            or message is None
            or message.role != "USER"
            or message.created_by_membership_id != self.actor.membership_id
        ):
            raise RetrievalPolicyError("RETRIEVAL_RUN_UNAVAILABLE")
        try:
            await assert_context_live(
                self.session, self.actor.organization_id, "agent_runs", run_id
            )
            await assert_context_live(
                self.session, self.actor.organization_id, "orchestration_runs", parent.id
            )
        except ValueError:
            raise RetrievalPolicyError("RETRIEVAL_CONTEXT_EXPIRED") from None
        if (
            agent.agent_id != "retrieve"
            or agent.agent_version != "1.0.0"
            or agent.capability not in {"retrieval.search_web", "retrieval.summarize_web"}
        ):
            raise RetrievalPolicyError("RETRIEVAL_AGENT_BOUNDARY_INVALID")
        parent_agent = await self.session.scalar(
            select(AgentRunModel).where(
                AgentRunModel.organization_id == self.actor.organization_id,
                AgentRunModel.id == agent.parent_agent_run_id,
            )
        )
        if (
            parent_agent is None
            or parent_agent.agent_id != "orchestrator"
            or parent_agent.orchestration_run_id != parent.id
        ):
            raise RetrievalPolicyError("RETRIEVAL_AGENT_BOUNDARY_INVALID")
        from work_management_ai.agents.retrieve.contracts import RetrieveInput
        from work_management_ai.runtime.manifests import (
            canonical_manifest_fingerprint,
            load_agent_manifest_resource,
        )

        try:
            typed_input = RetrieveInput.model_validate(agent.typed_input).model_dump(mode="json")
        except ValueError:
            raise RetrievalPolicyError("RETRIEVAL_INPUT_INVALID") from None
        expected = canonical_manifest_fingerprint(
            load_agent_manifest_resource("work_management_ai.agents.retrieve", "agent.yaml")
        )
        if agent.manifest_fingerprint != expected:
            raise RetrievalPolicyError("RETRIEVAL_MANIFEST_MISMATCH")
        if agent.status not in ({"RUNNING", "COMPLETED"} if allow_terminal else {"RUNNING"}) or (
            not allow_terminal and parent.status != "RUNNING"
        ):
            raise RetrievalPolicyError("RETRIEVAL_RUN_STOPPED")
        now = cast(datetime, await self.session.scalar(text("SELECT clock_timestamp()")))
        if lease is not None and (lease_until is None or lease_until <= now):
            raise RetrievalPolicyError("RETRIEVAL_JOB_CLAIM_LOST")
        return RetrievalRunScope(
            run_id,
            parent.id,
            agent.budget,
            parent.budget,
            parent.usage,
            typed_input,
            "\n".join(
                str(block.get("text", ""))
                for block in message.content_blocks
                if block.get("kind") == "text"
            ),
            agent.started_at or agent.created_at,
            parent.started_at or parent.created_at,
            now,
            lease_until,
            await self.protected_terms(),
        )

    async def get_budget(self, run_id: UUID, *, lock: bool = True) -> RetrievalBudgetState | None:
        query = (
            select(RetrievalRunBudgetModel)
            .where(
                RetrievalRunBudgetModel.organization_id == self.actor.organization_id,
                RetrievalRunBudgetModel.agent_run_id == run_id,
            )
            .execution_options(populate_existing=True)
        )
        row = await self.session.scalar(query.with_for_update() if lock else query)
        return _budget(row) if row else None

    async def insert_budget(self, state: RetrievalBudgetState) -> None:
        self.session.add(
            RetrievalRunBudgetModel(
                organization_id=state.organization_id,
                agent_run_id=state.agent_run_id,
                owner_membership_id=state.owner_membership_id,
                orchestration_run_id=state.orchestration_run_id,
                job_id=state.lease.job_id,
                worker_id=state.lease.worker_id,
                job_attempt=state.lease.attempt_no,
                fencing_token=state.fencing_token,
                scope=state.scope,
                caps=state.caps,
                reserved=state.reserved,
                started_at=state.started_at,
                deadline=state.deadline,
                expires_at=state.expires_at,
                version=state.version,
            )
        )
        await self.session.flush()

    async def save_budget(self, state: RetrievalBudgetState) -> None:
        row = await self.session.scalar(
            select(RetrievalRunBudgetModel)
            .where(
                RetrievalRunBudgetModel.organization_id == state.organization_id,
                RetrievalRunBudgetModel.agent_run_id == state.agent_run_id,
            )
            .with_for_update()
        )
        if row is None or row.version + 1 != state.version:
            raise RetrievalPolicyError("RETRIEVAL_VERSION_CONFLICT")
        row.reserved = dict(state.reserved)
        row.version = state.version
        row.worker_id = state.lease.worker_id
        row.job_attempt = state.lease.attempt_no
        row.fencing_token = state.fencing_token
        await self.session.flush()

    async def charge_parent(self, scope: RetrievalRunScope, charge: dict[str, int]) -> None:
        row = await self.session.get(OrchestrationRunModel, scope.orchestration_run_id)
        if row is None:
            raise RetrievalPolicyError("RETRIEVAL_RUN_UNAVAILABLE")
        usage = dict(row.usage)
        for key, (_, usage_key) in PARENT_KEYS.items():
            usage[usage_key] = usage[usage_key] + charge[key]
        row.usage = usage
        await self.session.flush()

    async def get_attempt(
        self, run_id: UUID, operation: str, attempt: int
    ) -> RetrievalAttempt | None:
        row = await self.session.scalar(
            select(RetrievalAttemptModel)
            .where(
                RetrievalAttemptModel.organization_id == self.actor.organization_id,
                RetrievalAttemptModel.agent_run_id == run_id,
                RetrievalAttemptModel.operation_key == operation,
                RetrievalAttemptModel.attempt_no == attempt,
            )
            .with_for_update()
        )
        if row is None:
            return None
        receipt = RetrievalReservation(
            row.id,
            row.organization_id,
            row.owner_membership_id,
            row.agent_run_id,
            row.operation_key,
            row.attempt_no,
            row.fencing_token,
            row.deadline,
            row.kind,
        )
        return RetrievalAttempt(
            receipt, row.request_hash, row.charged, row.status, row.dispatched, row.metadata_payload
        )

    async def insert_attempt(self, attempt: RetrievalAttempt) -> None:
        receipt = attempt.receipt
        self.session.add(
            RetrievalAttemptModel(
                organization_id=receipt.organization_id,
                id=receipt.id,
                agent_run_id=receipt.agent_run_id,
                owner_membership_id=receipt.membership_id,
                operation_key=receipt.operation_key,
                attempt_no=receipt.attempt_no,
                kind=receipt.kind,
                fencing_token=receipt.fencing_token,
                deadline=receipt.deadline,
                request_hash=attempt.request_hash,
                charged=attempt.charged,
                status=attempt.status,
                dispatched=False,
            )
        )
        await self.session.flush()

    async def _receipt_row(self, receipt: RetrievalReservation) -> RetrievalAttemptModel:
        row = await self.session.scalar(
            select(RetrievalAttemptModel)
            .where(
                RetrievalAttemptModel.organization_id == self.actor.organization_id,
                RetrievalAttemptModel.id == receipt.id,
            )
            .with_for_update()
        )
        if row is None or row.status != "RESERVED":
            raise RetrievalPolicyError("RETRIEVAL_ATTEMPT_UNAVAILABLE")
        return row

    async def dispatch(self, receipt: RetrievalReservation) -> None:
        row = await self._receipt_row(receipt)
        if row.dispatched:
            raise RetrievalPolicyError("RETRIEVAL_ATTEMPT_IN_PROGRESS")
        row.dispatched = True
        await self.session.flush()

    async def finish_attempt(
        self, receipt: RetrievalReservation, metadata: dict[str, Any], *, unknown: bool = False
    ) -> None:
        row = await self._receipt_row(receipt)
        row.status = "UNKNOWN" if unknown else "COMPLETED"
        row.metadata_payload = dict(metadata)
        row.cost_usd = (
            Decimal(str(metadata["cost_usd"])) if metadata.get("cost_usd") is not None else None
        )
        row.completed_at = cast(
            datetime, await self.session.scalar(text("SELECT clock_timestamp()"))
        )
        await self.session.flush()

    async def mark_unknown(self, run_id: UUID) -> None:
        rows = (
            await self.session.scalars(
                select(RetrievalAttemptModel)
                .where(
                    RetrievalAttemptModel.organization_id == self.actor.organization_id,
                    RetrievalAttemptModel.agent_run_id == run_id,
                    RetrievalAttemptModel.status == "RESERVED",
                )
                .with_for_update()
            )
        ).all()
        for row in rows:
            row.status = "UNKNOWN"
            row.metadata_payload = {"safe_code": "RETRIEVAL_RESULT_UNKNOWN"}
            row.completed_at = cast(
                datetime, await self.session.scalar(text("SELECT clock_timestamp()"))
            )
        await self.session.flush()

    async def has_unknown(self, run_id: UUID) -> bool:
        return (
            await self.session.scalar(
                select(RetrievalAttemptModel.id)
                .where(
                    RetrievalAttemptModel.organization_id == self.actor.organization_id,
                    RetrievalAttemptModel.agent_run_id == run_id,
                    RetrievalAttemptModel.status == "UNKNOWN",
                )
                .limit(1)
            )
        ) is not None

    async def store_sources(
        self, state: RetrievalBudgetState, invocation_id: UUID, sources: tuple[RetrievedSource, ...]
    ) -> tuple[RetrievalSnapshot, ...]:
        invocation = await self.session.scalar(
            select(ToolInvocationModel).where(
                ToolInvocationModel.organization_id == self.actor.organization_id,
                ToolInvocationModel.agent_run_id == state.agent_run_id,
                ToolInvocationModel.id == invocation_id,
                ToolInvocationModel.status == "RUNNING",
            )
        )
        if invocation is None:
            raise RetrievalPolicyError("RETRIEVAL_INVOCATION_UNAVAILABLE")
        existing = (
            await self.session.scalars(
                select(RetrievalSourceSnapshotModel).where(
                    RetrievalSourceSnapshotModel.organization_id == self.actor.organization_id,
                    RetrievalSourceSnapshotModel.agent_run_id == state.agent_run_id,
                )
            )
        ).all()
        by_id = {row.id: row for row in existing}
        selected: list[RetrievalSnapshot] = []
        for source in sources:
            identity = uuid5(
                NAMESPACE_URL,
                f"web:{self.actor.organization_id}:{state.agent_run_id}:{invocation_id}:{source.url.value}:{source.fingerprint}",
            )
            if identity in by_id:
                selected.append(_snapshot(by_id[identity]))
                continue
            if len(by_id) >= min(8, state.scope.get("limits", {}).get("max_sources", 8)):
                break
            payload = {
                "title": source.title,
                "text": source.text,
                "method": source.method,
                "published_at": source.published_at.isoformat() if source.published_at else None,
                "source_fetched_at": source.source_fetched_at.isoformat()
                if source.source_fetched_at
                else None,
                "provider_version": source.provider_version,
            }
            row = RetrievalSourceSnapshotModel(
                organization_id=self.actor.organization_id,
                id=identity,
                owner_membership_id=self.actor.membership_id,
                agent_run_id=state.agent_run_id,
                tool_invocation_id=invocation_id,
                canonical_url=source.url.value,
                fingerprint=source.fingerprint,
                payload=payload,
                observed_at=source.observed_at,
                expires_at=min(state.expires_at, source.observed_at + timedelta(days=30)),
            )
            self.session.add(row)
            by_id[identity] = row
            selected.append(_snapshot(row))
        await self.session.flush()
        return tuple(selected)

    async def load_sources(self, refs: tuple[UUID, ...]) -> tuple[RetrievalSnapshot, ...]:
        rows = (
            await self.session.scalars(
                select(RetrievalSourceSnapshotModel).where(
                    RetrievalSourceSnapshotModel.organization_id == self.actor.organization_id,
                    RetrievalSourceSnapshotModel.id.in_(refs),
                )
            )
        ).all()
        by_id = {row.id: row for row in rows}
        if any(ref not in by_id for ref in refs):
            raise RetrievalPolicyError("RETRIEVAL_SOURCE_UNAVAILABLE")
        for row in rows:
            try:
                await assert_context_live(
                    self.session, self.actor.organization_id, "agent_runs", row.agent_run_id
                )
            except ValueError:
                raise RetrievalPolicyError("RETRIEVAL_CONTEXT_EXPIRED") from None
            if not row.payload:
                raise RetrievalPolicyError("RETRIEVAL_CONTEXT_EXPIRED")
        return tuple(_snapshot(by_id[ref]) for ref in refs)

    async def source_url_allowed(self, run_id: UUID, url: str) -> bool:
        now = await self.session.scalar(text("SELECT clock_timestamp()"))
        return (
            await self.session.scalar(
                select(RetrievalSourceSnapshotModel.id)
                .where(
                    RetrievalSourceSnapshotModel.organization_id == self.actor.organization_id,
                    RetrievalSourceSnapshotModel.agent_run_id == run_id,
                    RetrievalSourceSnapshotModel.canonical_url == url,
                    RetrievalSourceSnapshotModel.expires_at > now,
                )
                .limit(1)
            )
        ) is not None

    async def audit(self, action: str, run_id: UUID | None, *, code: str | None = None) -> None:
        self.session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=self.actor.organization_id,
                actor_membership_id=self.actor.membership_id,
                action=action,
                outcome=AuditOutcome.REJECTED if code else AuditOutcome.SUCCEEDED,
                resource_type="RETRIEVAL_RUN",
                resource_id=run_id,
                request_id=str(uuid4()),
                before_data={},
                after_data={},
                reason_data={"code": code} if code else {},
            )
        )
        await self.session.flush()

    async def load_checkpoint(self, run_id: UUID) -> dict[str, Any] | None:
        from app.modules.assistant.adapters.database_models import AgentCheckpointModel

        row = await self.session.scalar(
            select(AgentCheckpointModel)
            .where(
                AgentCheckpointModel.organization_id == self.actor.organization_id,
                AgentCheckpointModel.agent_run_id == run_id,
                AgentCheckpointModel.checkpoint_version == "retrieve.web.v1",
            )
            .order_by(AgentCheckpointModel.sequence.desc())
            .limit(1)
        )
        return dict(row.typed_state) if row else None

    async def save_checkpoint(self, run_id: UUID, value: dict[str, Any]) -> None:
        from uuid import uuid4

        from app.modules.assistant.adapters.database_models import (
            AgentCheckpointModel,
            AgentRunModel,
        )

        previous = await self.load_checkpoint(run_id)
        if (
            type(value.get("version")) is not int
            or value["version"] != (previous["version"] if previous else 0) + 1
        ):
            raise RetrievalPolicyError("RETRIEVAL_CHECKPOINT_CONFLICT")
        run = await self.session.scalar(
            select(AgentRunModel).where(
                AgentRunModel.organization_id == self.actor.organization_id,
                AgentRunModel.id == run_id,
            )
        )
        if run is None:
            raise RetrievalPolicyError("RETRIEVAL_RUN_UNAVAILABLE")
        self.session.add(
            AgentCheckpointModel(
                id=uuid4(),
                organization_id=self.actor.organization_id,
                orchestration_run_id=run.orchestration_run_id,
                agent_run_id=run_id,
                sequence=value["version"],
                node="retrieve",
                typed_state=value,
                checkpoint_version="retrieve.web.v1",
            )
        )

    async def protected_terms(self) -> tuple[str, ...]:
        return tuple(
            str(value)
            for value in (
                await self.session.execute(
                    text(
                        "SELECT name FROM projects WHERE organization_id=:org "
                        "UNION SELECT title FROM tasks WHERE organization_id=:org"
                    ),
                    {"org": self.actor.organization_id},
                )
            ).scalars()
        )
