"""Bilingual regression evaluation using the real Orchestrator and Retrieve graphs.

Synthetic external boundaries exercise contracts; they do not measure held-out model
accuracy and never authorize application activation or access to business data.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, computed_field

from work_management_ai.agents.orchestrator.contracts import (
    ActiveConversationContext,
    ActivePlanningContext,
    OrchestratorInput,
)
from work_management_ai.agents.orchestrator.harness import OrchestratorHarness
from work_management_ai.agents.retrieve.budget import ModelReservation, RetrievalCheckpoint
from work_management_ai.agents.retrieve.contracts import PublicURL, RetrieveOutput, WebEvidence
from work_management_ai.agents.retrieve.harness import RetrieveHarness
from work_management_ai.model_gateway.contracts import ModelGateway, ModelUsage
from work_management_ai.model_gateway.errors import ModelTimeoutError
from work_management_ai.model_gateway.mock import MockModelGateway
from work_management_ai.runtime.activation import ActivationContext
from work_management_ai.runtime.agent_registry import AgentRegistry
from work_management_ai.runtime.contracts import (
    ActorReference,
    AgentHandoff,
    AgentId,
    AgentResult,
    AgentRunStatus,
    ContextReference,
    JsonValue,
    ResolvedActorContext,
    ToolExecutionRequest,
    ToolExecutionResult,
)
from work_management_ai.runtime.manifests import SkillManifest, ToolManifest, load_yaml_resource
from work_management_ai.runtime.policy_guard import PolicyGuard
from work_management_ai.runtime.skill_registry import SkillRegistry
from work_management_ai.runtime.tool_registry import ToolRegistry
from work_management_ai.tools.web.contracts import (
    WebReadPagesOutput,
    WebRevalidateInput,
    WebRevalidateOutput,
    WebSearchOutput,
)

_ROOT = Path(__file__).resolve().parents[4]
_FIXTURES = _ROOT / "ai/tests/fixtures/retrieve_web"
_REQUIRED = frozenset((f"W{i:02}", locale) for i in (*range(1, 14), 18) for locale in ("vi", "en"))
_ACTIVATION = ActivationContext(5, True, frozenset({"I3"}), True)


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    case_id: str
    locale: Literal["vi", "en"]
    scenario: str
    message: str
    query: str
    source_mode: Literal["WEB", "DOCUMENTS", "BOTH"]
    answer_mode: Literal["SUMMARY", "EVIDENCE_ONLY"]
    expected_route: str
    expected_outcome: str
    status: Literal["ACTIVE", "DEFERRED_I2"] = "ACTIVE"


class Observation(BaseModel):
    case_id: str
    locale: Literal["vi", "en"]
    status: Literal["ACTIVE", "DEFERRED_I2"]
    mode: Literal["MOCK", "HOSTED"] = "MOCK"
    expected_route: str
    actual_route: str = "none"
    expected_outcome: str
    actual_outcome: str = "DEFERRED_I2"
    assertions: dict[str, bool] = Field(default_factory=dict)
    orchestrator_runs: int = 0
    retrieve_runs: int = 0
    synthesis_attempts: int = 0
    tool_calls: int = 0
    claim_count: int = 0
    source_count: int = 0
    safety_violations: int = 0
    duration_ms: int = 0
    source_hashes: tuple[str, ...] = ()
    observed_at: datetime | None = None
    run_id: UUID | None = None

    @computed_field
    @property
    def passed(self) -> bool:
        required = {"route", "outcome", "citation_bindings", "safety", "private_query", "budgets"}
        required |= {
            "W02": {"page_read"},
            "W03": {"no_synthesis"},
            "W10": {"verified_evidence_fallback"},
            "W11": {"verified_evidence_fallback"},
            "W12": {"conflict_and_unknown_freshness"},
        }.get(self.case_id, set())
        return (
            self.status == "ACTIVE"
            and required <= self.assertions.keys()
            and all(self.assertions.values())
            and self.orchestrator_runs >= 1
            and (self.actual_route != "retrieve" or self.retrieve_runs == 1)
        )


class RetrieveEvaluationReport(BaseModel):
    rows: tuple[Observation, ...]
    required_total: int = 28
    hosted_status: Literal["NOT_RUN"] = "NOT_RUN"
    hosted_ready: Literal[False] = False
    held_out_routing_accuracy: float | None = None
    semantic_support_accuracy: float | None = None
    top5_relevance: float | None = None
    answerable_evidence_coverage: float | None = None
    freshness_accuracy: float | None = None
    label_status: str = "AUTHORED_SYNTHETIC_NOT_HUMAN_REVIEWED_HELD_OUT"
    versions: dict[str, str] = Field(
        default_factory=lambda: {
            "workflow": "retrieve.web.v1",
            "verifier": "retrieve_grounding@1",
            "prompt": "retrieve.synthesis.v1",
            "fixture": "retrieve-regression.v1",
        }
    )

    @computed_field
    @property
    def safety_violations(self) -> int:
        return sum(row.safety_violations for row in self.rows)

    @computed_field
    @property
    def passed(self) -> bool:
        active = [row for row in self.rows if row.status == "ACTIVE"]
        keys = {(row.case_id, row.locale) for row in active}
        return (
            keys >= _REQUIRED
            and len(keys) == len(active)
            and all(row.passed for row in active)
            and self.safety_violations == 0
        )


def load_cases(path: Path | None = None) -> tuple[Case, ...]:
    data = json.loads((path or _FIXTURES / "routing_cases.json").read_text())
    return tuple(Case.model_validate(row) for row in data["cases"])


def _registry() -> AgentRegistry:
    skills = SkillRegistry(
        load_yaml_resource(f"work_management_ai.skills.{name}", "skill.yaml", SkillManifest)
        for name in (
            "answer_work_question",
            "create_project_plan",
            "revise_project_plan",
            "web_search_and_summarize",
        )
    )
    resources = [
        ("work_management_ai.tools.automation", "tool.yaml"),
        ("work_management_ai.tools.risk", "tool.yaml"),
        ("work_management_ai.tools.work.read_my_tasks", "tool.yaml"),
        ("work_management_ai.tools.work.read_resource", "tool.yaml"),
        ("work_management_ai.tools.planning.manage_run", "tool.yaml"),
        *(
            ("work_management_ai.tools.web", name)
            for name in ("search.yaml", "read_pages.yaml", "revalidate.yaml")
        ),
    ]
    tools = ToolRegistry(
        load_yaml_resource(package, name, ToolManifest) for package, name in resources
    )
    registry = AgentRegistry(
        skill_registry=skills,
        tool_registry=tools,
        evaluator_ids=frozenset(
            {
                "orchestrator_plan@1",
                "work_grounding@1",
                "planning_schema@1",
                "planning_invariants@1",
                "planning_grounding@1",
                "retrieve_grounding@1",
            }
        ),
    )
    for name in ("orchestrator", "work_intelligence", "planning", "retrieve"):
        registry.register_resource(f"work_management_ai.agents.{name}", "agent.yaml")
    return registry


class _Actors:
    def __init__(self, actor: ActorReference):
        self.actor = ResolvedActorContext(**actor.model_dump(), role="MANAGER", is_active=True)

    async def resolve(self, reference: ActorReference) -> ResolvedActorContext:
        return self.actor.model_copy(
            update={
                "is_active": reference
                == ActorReference(
                    organization_id=self.actor.organization_id,
                    membership_id=self.actor.membership_id,
                )
            }
        )


class EvaluationUsage:
    """Ephemeral evaluation storage only; production always uses canonical SQL ports."""

    def __init__(self):
        self.checkpoint: RetrievalCheckpoint | None = None
        self.started = monotonic()
        self.reservations: set[int] = set()

    async def remaining_seconds(self, *, retrieval: bool = False) -> float:
        return max(0, (8 if retrieval else 25) - (monotonic() - self.started))

    async def load_checkpoint(self) -> RetrievalCheckpoint | None:
        return self.checkpoint.model_copy(deep=True) if self.checkpoint else None

    async def save_checkpoint(self, checkpoint: RetrievalCheckpoint) -> None:
        if self.checkpoint and checkpoint.version != self.checkpoint.version + 1:
            raise ValueError("EVALUATION_CHECKPOINT_CAS")
        self.checkpoint = checkpoint.model_copy(deep=True)

    async def reserve_node(self, operation_key: str) -> None:
        del operation_key

    async def reserve_model_attempt(
        self, *, attempt_no: int, input_tokens: int, output_tokens: int
    ) -> ModelReservation:
        if (
            attempt_no in self.reservations
            or not 1 <= attempt_no <= 2
            or input_tokens > 12000
            or output_tokens > 1600
        ):
            raise ValueError("EVALUATION_MODEL_BUDGET")
        self.reservations.add(attempt_no)
        return ModelReservation(reservation_id=str(attempt_no), attempt_no=attempt_no)

    async def record_model_usage(
        self,
        reservation: ModelReservation,
        usage: ModelUsage | None,
        *,
        safe_code: str | None = None,
    ) -> None:
        del reservation, usage, safe_code


def _sources(case: Case) -> tuple[WebEvidence, ...]:
    raw = json.loads((_FIXTURES / "source_fixtures.json").read_text())
    now = datetime.now(UTC)
    result: list[WebEvidence] = []
    for item in raw["sources"]:
        if hashlib.sha256(item["excerpt"].encode()).hexdigest() != item["fingerprint"]:
            raise ValueError("SOURCE_FIXTURE_HASH_MISMATCH")
        if case.scenario == "injection":
            item["excerpt"] += "\n" + raw["injection"]
            item["fingerprint"] = hashlib.sha256(item["excerpt"].encode()).hexdigest()
        result.append(
            WebEvidence.model_validate(
                {
                    **item,
                    "observed_at": now,
                    "expires_at": now + timedelta(days=1),
                    "provider_version": "synthetic.web.v1",
                }
            )
        )
    return tuple(
        result if case.scenario in {"multiple_pages", "partial_page", "conflict"} else result[:1]
    )


class EvaluationTools:
    def __init__(self, case: Case, actor: ActorReference):
        self.case, self.actor = case, actor
        self.sources: tuple[WebEvidence, ...] = _sources(case)
        self.requests: list[ToolExecutionRequest] = []
        self.by_id: dict[UUID, WebEvidence] = {s.evidence_id: s for s in self.sources}

    def _refs(self, sources: Sequence[WebEvidence]) -> tuple[ContextReference, ...]:
        return tuple(
            ContextReference(
                reference_id=s.evidence_id,
                resource_id=s.evidence_id,
                resource_type="WEB_SOURCE_SNAPSHOT",
                organization_id=self.actor.organization_id,
                version=1,
                fingerprint=s.fingerprint,
                observed_at=s.observed_at,
            )
            for s in sources
        )

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        self.requests.append(request)
        if request.actor != self.actor:
            return ToolExecutionResult(
                status="REJECTED", typed_output={}, safe_error_code="TENANT_MISMATCH"
            )
        sources: tuple[WebEvidence, ...] = ()
        if request.tool_id == "web.search":
            if self.case.scenario == "outage":
                return ToolExecutionResult(
                    status="FAILED", typed_output={}, safe_error_code="RETRIEVAL_RATE_LIMITED"
                )
            sources = () if self.case.scenario == "empty" else self.sources
            data = WebSearchOutput(
                evidence=sources,
                limitations=("NOT_FOUND",) if self.case.scenario == "empty" else (),
            )
        elif request.tool_id == "web.read_pages":
            urls = request.typed_input["urls"]
            if self.case.scenario == "partial_page" and self.sources[-1].canonical_url in cast(
                list[str], urls
            ):
                data = WebReadPagesOutput(evidence=(), limitations=("CACHE_UNAVAILABLE",))
            else:
                sources = tuple(
                    source.model_copy(
                        update={
                            "method": "PAGE_CONTENT",
                            "evidence_id": uuid5(NAMESPACE_URL, f"page:{source.evidence_id}"),
                        }
                    )
                    for source in self.sources
                    if source.canonical_url in cast(list[str], urls)
                )
                self.by_id.update({source.evidence_id: source for source in sources})
                data = WebReadPagesOutput(evidence=sources)
        elif request.tool_id == "web.revalidate":
            wanted = WebRevalidateInput.model_validate(request.typed_input)
            sources = tuple(
                self.by_id[ref.evidence_id]
                for ref in wanted.sources
                if ref.evidence_id in self.by_id
                and self.by_id[ref.evidence_id].fingerprint == ref.fingerprint
            )
            data = WebRevalidateOutput(
                valid_evidence_ids=tuple(s.evidence_id for s in sources),
                observed_at=datetime.now(UTC),
            )
        else:
            return ToolExecutionResult(
                status="REJECTED", typed_output={}, safe_error_code="FORBIDDEN_TOOL"
            )
        return ToolExecutionResult(
            status="SUCCEEDED",
            typed_output=cast(dict[str, JsonValue], data.model_dump(mode="json")),
            evidence=self._refs(sources),
        )


def _gateway(case: Case, tools: EvaluationTools) -> MockModelGateway:
    query = case.message if case.scenario in {"documents", "both"} else case.query
    if case.scenario == "private_context":
        query = "PRIVATE_CONTEXT_MUST_NOT_LEAVE"
    start = max(0, case.message.find(query))
    intent = dict(
        search_queries=[query],
        message_spans=[dict(start=start, end=start + len(query), text=query)],
        source_mode=case.source_mode,
        answer_mode=case.answer_mode,
        requirements=[dict(requirement_id="topic", description="Public guidance")],
    )
    plan: dict[str, object] = dict(
        objectives=["Evaluate source-scoped behavior"],
        response_language=case.locale,
        steps=[
            dict(
                step_id="retrieve-1",
                target_agent_id="retrieve",
                target_agent_version="1.0.0",
                capability="retrieval.search_web",
                objective="Public guidance",
                typed_input=intent,
                mode="READ_ONLY",
            )
        ],
    )
    if case.scenario == "missing_goal":
        plan.update(
            steps=[],
            goal_question=dict(
                question="Bạn muốn đạt mục tiêu gì?"
                if case.locale == "vi"
                else "What goal should the plan achieve?"
            ),
        )
    elif case.scenario == "active_draft":
        plan.update(
            steps=[
                dict(
                    step_id="revise_plan",
                    target_agent_id="planning",
                    target_agent_version="1.0.0",
                    capability="planning.revise",
                    objective="Revise existing draft",
                    typed_input={},
                    mode="PROPOSAL",
                )
            ],
            planning_intent="CONTINUE_DRAFT",
        )
    elif case.scenario == "work_deadline":
        plan.update(
            steps=[
                dict(
                    step_id="work",
                    target_agent_id="work_intelligence",
                    target_agent_version="1.0.0",
                    capability="work.answer_question",
                    objective="Read current deadlines",
                    typed_input={},
                    mode="READ_ONLY",
                )
            ]
        )

    labels = json.loads((_FIXTURES / "grounding_cases.json").read_text())
    first_id = tools.sources[0].evidence_id
    if case.scenario in {"multiple_pages", "partial_page"}:
        first_id = uuid5(NAMESPACE_URL, f"page:{first_id}")
    claims = [dict(text=labels["claims"][case.locale], evidence_ids=[str(first_id)])]
    summary: object = dict(claims=claims, limitations=[], insufficient_evidence=False)
    if case.scenario == "unknown_citation":
        summary = dict(
            claims=[
                dict(
                    text="Unsupported claim", evidence_ids=["99999999-9999-4999-8999-999999999999"]
                )
            ]
        )
    elif case.scenario == "conflict":
        claims.append(
            dict(
                text=labels["conflict_claims"][case.locale],
                evidence_ids=[str(tools.sources[1].evidence_id)],
            )
        )
        summary = dict(claims=claims, limitations=["SOURCES_DISAGREE"], insufficient_evidence=False)
    fixtures = {
        f"orchestrator.{case.locale}.plan": plan,
        f"orchestrator.{case.locale}.synthesize": dict(
            blocks=[dict(kind="text", text="Verified permitted work facts.")]
        ),
        "retrieve.synthesis.v1": summary,
    }
    sequence = (
        {
            "retrieve.synthesis.v1": (
                ModelTimeoutError("synthetic timeout"),
                ModelTimeoutError("synthetic timeout"),
            )
        }
        if case.scenario == "model_timeout"
        else None
    )
    return MockModelGateway(
        fixtures=fixtures,
        sequence_fixtures=sequence,
        usage_fixtures={"retrieve.synthesis.v1": ModelUsage(100, 50)},
    )


class _PublicPolicy:
    async def validate_public_intent(
        self, intent: dict[str, Any], message: str, reference: ActorReference
    ) -> None:
        del message, reference
        for query in intent.get("search_queries", []):
            for url in re.findall(r"https?://\S+", query):
                TypeAdapter(PublicURL).validate_python(url)


class _Specialists:
    def __init__(self, actor: _Actors, gateway: ModelGateway, tools: EvaluationTools):
        self.actor, self.gateway, self.tools = actor, gateway, tools
        self.handoffs: list[AgentHandoff] = []
        self.results: list[AgentResult] = []
        self.retrieve_runs = 0

    async def run_specialist(self, handoff: AgentHandoff) -> AgentResult:
        self.handoffs.append(handoff)
        if handoff.target_agent_id == AgentId.RETRIEVE:
            self.retrieve_runs += 1
            result = await RetrieveHarness(
                model_gateway=self.gateway,
                tool_executor=self.tools,
                usage_port=EvaluationUsage(),
                actor_resolver=self.actor,
                activation=_ACTIVATION,
            ).run(handoff)
        else:
            # Only delegation is measured for these extra non-web regression rows.
            # Their real application effects are covered by Core/SQL suites, not fabricated here.
            result = AgentResult(
                agent_id=handoff.target_agent_id,
                agent_version=handoff.target_agent_version,
                status=AgentRunStatus.COMPLETED,
                typed_output={"summary": "Permitted work fixture"},
                stop_reason="COMPLETED",
            )
        self.results.append(result)
        return result


async def run_retrieve_web_suite(
    cases: Sequence[Case],
    gateway: ModelGateway | None = None,
    tools: Callable[[Case, ActorReference], EvaluationTools] | None = None,
) -> RetrieveEvaluationReport:
    if gateway is not None and not isinstance(gateway, MockModelGateway):
        raise ValueError("MOCK_GATEWAY_REQUIRED")
    rows: list[Observation] = []
    registry = _registry()
    for case in cases:
        base: dict[str, Any] = dict(
            case_id=case.case_id,
            locale=case.locale,
            status=case.status,
            expected_route=case.expected_route,
            expected_outcome=case.expected_outcome,
        )
        if case.status == "DEFERRED_I2":
            rows.append(Observation(**base))
            continue
        identity = uuid5(NAMESPACE_URL, f"retrieve-evaluation:{case.case_id}:{case.locale}")
        actor = ActorReference(
            organization_id=uuid5(identity, "organization"),
            membership_id=uuid5(identity, "membership"),
        )
        actor_port = _Actors(actor)
        executor = tools(case, actor) if tools else EvaluationTools(case, actor)
        model = gateway or _gateway(case, executor)
        runner = _Specialists(actor_port, model, executor)
        active = ActiveConversationContext(recent_messages=())
        if case.scenario == "private_context":
            active = ActiveConversationContext.model_validate(
                dict(
                    recent_messages=[dict(role="ASSISTANT", text="PRIVATE_CONTEXT_MUST_NOT_LEAVE")]
                )
            )
        if case.scenario == "active_draft":
            active = ActiveConversationContext(
                recent_messages=(),
                active_planning=ActivePlanningContext(
                    workflow_run_id=uuid5(identity, "workflow"),
                    workflow_status="WAITING_FOR_INPUT",
                    proposal_id=uuid5(identity, "proposal"),
                    proposal_version=1,
                    proposal_status="DRAFT",
                    requested_operation="REVISE",
                ),
            )
        started = monotonic()
        output = await OrchestratorHarness(
            model_gateway=model,
            registry=registry,
            policy_guard=PolicyGuard(),
            actor_resolver=actor_port,
            specialists=runner,
            activation=_ACTIVATION,
            public_query_validator=_PublicPolicy(),
        ).run_turn(
            OrchestratorInput(
                conversation_id=uuid5(identity, "conversation"),
                turn_id=identity,
                locale=case.locale,
                message=case.message,
                actor=actor,
                active_context=active,
            )
        )
        route = runner.handoffs[0].target_agent_id.value if runner.handoffs else "none"
        retrieval = next(
            (
                RetrieveOutput.model_validate(result.typed_output)
                for result in runner.results
                if result.agent_id == AgentId.RETRIEVE and result.typed_output
            ),
            None,
        )
        actual = (
            retrieval.outcome.value
            if retrieval
            else "CAPABILITY_UNAVAILABLE"
            if any(block.kind == "capability_unavailable" for block in output.blocks)
            else "NEEDS_INPUT"
            if output.status.value == "AWAITING_INPUT"
            else "NON_WEB"
        )
        sources = retrieval.evidence if retrieval else ()
        claims = retrieval.summary.claims if retrieval and retrieval.summary else ()
        hashes = {source.evidence_id: source.fingerprint for source in sources}
        citations = all(set(claim.evidence_ids) <= hashes.keys() for claim in claims) and all(
            hashlib.sha256(source.excerpt.encode()).hexdigest() == source.fingerprint
            for source in sources
        )
        safe = all(
            request.actor == actor
            and request.tool_id in {"web.search", "web.read_pages", "web.revalidate"}
            for request in executor.requests
        ) and all(
            not result.proposed_actions and result.requested_handoff is None
            for result in runner.results
        )
        current_query_only = all(
            "PRIVATE_CONTEXT_MUST_NOT_LEAVE" not in str(request.typed_input)
            for request in executor.requests
        )
        assertions = dict(
            route=route == case.expected_route,
            outcome=actual == case.expected_outcome,
            citation_bindings=citations,
            safety=safe,
            private_query=current_query_only,
            budgets=all(
                result.tool_calls_used <= 8 and result.model_attempts_used <= 2
                for result in runner.results
            ),
        )
        if case.scenario == "evidence_only":
            assertions["no_synthesis"] = bool(retrieval and retrieval.usage.model_attempts == 0)
        if case.scenario == "multiple_pages":
            assertions["page_read"] = any(
                request.tool_id == "web.read_pages" for request in executor.requests
            )
        if case.scenario == "conflict":
            # Author-supplied fixture dates are independent of the observed tool/model output.
            expected_dates = {
                source.canonical_url: (source.published_at, source.source_fetched_at)
                for source in _sources(case)
            }
            assertions["conflict_and_unknown_freshness"] = bool(
                retrieval
                and sources
                and "SOURCES_DISAGREE" in retrieval.limitations
                and all(
                    (source.published_at, source.source_fetched_at)
                    == expected_dates.get(source.canonical_url)
                    for source in sources
                )
            )
        if case.scenario in {"unknown_citation", "model_timeout"}:
            assertions["verified_evidence_fallback"] = bool(sources and not claims)
        rows.append(
            Observation(
                **base,
                actual_route=route,
                actual_outcome=actual,
                assertions=assertions,
                orchestrator_runs=1,
                retrieve_runs=runner.retrieve_runs,
                synthesis_attempts=retrieval.usage.model_attempts if retrieval else 0,
                tool_calls=len(executor.requests),
                claim_count=len(claims),
                source_count=len(sources),
                safety_violations=int(not safe or not current_query_only),
                duration_ms=int((monotonic() - started) * 1000),
                source_hashes=tuple(source.fingerprint for source in sources),
                observed_at=datetime.now(UTC),
                run_id=identity,
            )
        )
    return RetrieveEvaluationReport(rows=tuple(rows))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = asyncio.run(run_retrieve_web_suite(load_cases(args.cases)))
    text = report.model_dump_json(indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
    print(text)
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
