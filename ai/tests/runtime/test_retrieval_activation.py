from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from pydantic import ValidationError

from work_management_ai.runtime.agent_registry import AgentRegistry, AgentRegistryError
from work_management_ai.runtime.contracts import (
    ActorReference,
    AgentBudget,
    AgentHandoff,
    AgentId,
    ResolvedActorContext,
)
from work_management_ai.runtime.manifests import (
    AgentManifest,
    SkillManifest,
    ToolManifest,
    canonical_manifest_fingerprint,
    load_yaml_resource,
)
from work_management_ai.runtime.policy_guard import AgentPolicyError, PolicyGuard
from work_management_ai.runtime.skill_registry import SkillRegistry
from work_management_ai.runtime.tool_registry import ToolRegistry

if TYPE_CHECKING:
    from work_management_ai.runtime.activation import ActivationContext


def _activation() -> ActivationContext:
    from work_management_ai.runtime.activation import ActivationContext

    return ActivationContext(
        active_phase=5,
        core_exit_verified=True,
        active_tracks=frozenset({"I3"}),
        provider_ready=True,
    )


def _registry() -> AgentRegistry:
    tools = ToolRegistry(
        load_yaml_resource("work_management_ai.tools.web", name, ToolManifest)
        for name in ("search.yaml", "read_pages.yaml", "revalidate.yaml")
    )
    skills = SkillRegistry(
        [
            load_yaml_resource(
                "work_management_ai.skills.web_search_and_summarize", "skill.yaml", SkillManifest
            )
        ]
    )
    registry = AgentRegistry(
        skill_registry=skills,
        tool_registry=tools,
        evaluator_ids=frozenset({"retrieve_grounding@1"}),
    )
    registry.register_resource("work_management_ai.agents.retrieve", "agent.yaml")
    return registry


def _handoff() -> tuple[ResolvedActorContext, AgentHandoff]:
    actor = ResolvedActorContext(
        membership_id=uuid4(), organization_id=uuid4(), role="EMPLOYEE", is_active=True
    )
    return actor, AgentHandoff(
        orchestration_run_id=uuid4(),
        parent_agent_run_id=uuid4(),
        target_agent_id=AgentId.RETRIEVE,
        target_agent_version="1.0.0",
        capability="retrieval.search_web",
        objective="Find public guidance",
        typed_input={"query": "public guidance"},
        context_references=(),
        actor=ActorReference(
            membership_id=actor.membership_id, organization_id=actor.organization_id
        ),
        budget=AgentBudget(
            max_model_attempts=2,
            max_input_tokens=12000,
            max_output_tokens=3200,
            max_iterations=12,
            max_tool_calls=8,
            timeout_seconds=25,
        ),
        step_id="retrieve-1",
        idempotency_key="turn:retrieve-1",
    )


@pytest.mark.parametrize("missing", ["context", "core", "track", "provider", "phase"])
def test_web_requires_i3_core_and_provider(missing: str) -> None:
    from work_management_ai.runtime.activation import capability_enabled

    registry = _registry()
    activation = _activation()
    if missing == "core":
        activation = replace(activation, core_exit_verified=False)
    elif missing == "track":
        activation = replace(activation, active_tracks=frozenset({"I2"}))
    elif missing == "provider":
        activation = replace(activation, provider_ready=False)
    elif missing == "phase":
        activation = replace(activation, active_phase=4)
    context = None if missing == "context" else activation
    assert not capability_enabled("retrieval.search_web", context)
    assert registry.planning_catalog(active_phase=99, role="EMPLOYEE", activation=context) == ()
    with pytest.raises(AgentRegistryError, match="INACTIVE"):
        registry.resolve(AgentId.RETRIEVE, "1.0.0", active_phase=99, activation=context)
    manifest = registry.resolve(
        AgentId.RETRIEVE, "1.0.0", active_phase=5, activation=_activation()
    ).manifest
    actor, handoff = _handoff()
    with pytest.raises(AgentPolicyError, match="INACTIVE"):
        PolicyGuard().authorize_handoff(
            current_actor=actor,
            parent_agent_id=AgentId.ORCHESTRATOR,
            handoff=handoff,
            manifest=manifest,
            activation=context,
        )


def test_web_only_catalog_excludes_document_connectors() -> None:
    registry = _registry()
    catalog = registry.planning_catalog(active_phase=5, role="EMPLOYEE", activation=_activation())
    assert len(catalog) == 1
    assert catalog[0]["capabilities"] == ["retrieval.search_web", "retrieval.summarize_web"]
    manifest = registry.resolve(
        AgentId.RETRIEVE, "1.0.0", active_phase=5, activation=_activation()
    ).manifest
    actor, handoff = _handoff()
    PolicyGuard().authorize_handoff(
        current_actor=actor,
        parent_agent_id=AgentId.ORCHESTRATOR,
        handoff=handoff,
        manifest=manifest,
        requested_tool_ids=("web.search@1",),
        activation=_activation(),
    )
    for parent in [AgentId.RETRIEVE, AgentId.PLANNING]:
        with pytest.raises(AgentPolicyError, match="ORCHESTRATOR_REQUIRED"):
            PolicyGuard().authorize_handoff(
                current_actor=actor,
                parent_agent_id=parent,
                handoff=handoff,
                manifest=manifest,
                activation=_activation(),
            )
    with pytest.raises(AgentRegistryError, match="PHASE_INACTIVE"):
        registry.resolve(AgentId.RETRIEVE, "1.0.0", active_phase=4, activation=_activation())


@pytest.mark.parametrize(
    "key", ["activation", "core_exit_verified", "active_tracks", "provider_ready"]
)
def test_model_cannot_supply_activation(key: str) -> None:
    registry = _registry()
    manifest = registry.resolve(
        AgentId.RETRIEVE, "1.0.0", active_phase=5, activation=_activation()
    ).manifest
    actor, handoff = _handoff()
    forged = handoff.model_copy(update={"typed_input": {"query": "public", key: True}})
    with pytest.raises(AgentPolicyError, match="RESERVED_AUTHORITY_KEY"):
        PolicyGuard().authorize_handoff(
            current_actor=actor,
            parent_agent_id=AgentId.ORCHESTRATOR,
            handoff=forged,
            manifest=manifest,
            activation=_activation(),
        )


def test_budget_tool_and_tenant_escalation_are_denied() -> None:
    registry = _registry()
    manifest = registry.resolve(
        AgentId.RETRIEVE, "1.0.0", active_phase=5, activation=_activation()
    ).manifest
    actor, handoff = _handoff()
    for changed, code in [
        (
            handoff.model_copy(
                update={"budget": handoff.budget.model_copy(update={"max_handoffs": 1})}
            ),
            "BUDGET",
        ),
        (
            handoff.model_copy(
                update={
                    "actor": ActorReference(
                        membership_id=actor.membership_id, organization_id=uuid4()
                    )
                }
            ),
            "CONTEXT_MISMATCH",
        ),
    ]:
        with pytest.raises(AgentPolicyError, match=code):
            PolicyGuard().authorize_handoff(
                current_actor=actor,
                parent_agent_id=AgentId.ORCHESTRATOR,
                handoff=changed,
                manifest=manifest,
                activation=_activation(),
            )
    with pytest.raises(AgentPolicyError, match="TOOL_NOT_ALLOWED"):
        PolicyGuard().authorize_handoff(
            current_actor=actor,
            parent_agent_id=AgentId.ORCHESTRATOR,
            handoff=handoff,
            manifest=manifest,
            activation=_activation(),
            requested_tool_ids=("assignment.assign_task@1",),
        )


def test_retrieve_cannot_use_ungated_legacy_manifest() -> None:
    from work_management_ai.runtime.manifests import load_agent_manifest_resource

    manifest = load_agent_manifest_resource("work_management_ai.agents.retrieve", "agent.yaml")
    forged = manifest.model_dump(exclude={"activation", "retrieval_limits"})
    forged["schema_version"] = "1.0"
    with pytest.raises(ValidationError, match="capability-gated"):
        AgentManifest.model_validate(forged)


@pytest.mark.parametrize("field", ["activation", "capabilities", "runtime"])
def test_manifest_rejects_missing_gates_future_capabilities_and_larger_budgets(field: str) -> None:
    from pydantic import TypeAdapter

    from work_management_ai.runtime.manifests import (
        AnyAgentManifest,
        load_agent_manifest_resource,
    )

    manifest = load_agent_manifest_resource("work_management_ai.agents.retrieve", "agent.yaml")
    raw = manifest.model_dump()
    if field == "activation":
        raw.pop("activation")
    elif field == "capabilities":
        raw["capabilities"] = ["retrieval.search_web", "retrieval.search_external"]
    else:
        raw["runtime"] = {**manifest.runtime.model_dump(), "max_handoffs": 1}
    adapter: TypeAdapter[AnyAgentManifest] = TypeAdapter(AnyAgentManifest)
    with pytest.raises(ValidationError):
        adapter.validate_python(raw)


def test_legacy_core_manifests_still_load() -> None:
    expected = {
        "orchestrator": "34510dc199279165f220e1bd1d4c7ff0d3148157d7844d2511391b62ac16038f",
        "work_intelligence": "13211c3776afedad1310ce40718e305bbe2cf22531b760d494854895c761c7e1",
        "planning": "9cad9801ca3a4553c42de70ea92bce4e78a042de55ef696ceabbb5451e33f728",
        "assignment": "d439a21fe82fa4563b4a922454ce88cf27fcf801e09057962b7ab5b82e6829b8",
        "daily_update": "961d3c8181e5ad2acede54095bc4383da39d2191c8bf6e0e3039b86afa103170",
        "risk": "f4764425aad6afbc98dea6bfaeb45eb43489936fc79726cb9017534628c8d0f1",
        "reporting": "ff06120abec20215dd13e5cb581e5cc0e6671e7067601ca6080c8053432eccc7",
    }
    for name, fingerprint in expected.items():
        manifest = load_yaml_resource(
            f"work_management_ai.agents.{name}", "agent.yaml", AgentManifest
        )
        assert canonical_manifest_fingerprint(manifest) == fingerprint


@pytest.mark.asyncio
async def test_retrieve_trace_keeps_agent_identity_without_exporting_source_bodies() -> None:
    from work_management_ai.observability.safe_trace import TraceSpanRecord
    from work_management_ai.tracing import trace_session, trace_span

    records: list[TraceSpanRecord] = []

    class Sink:
        def emit(self, record: TraceSpanRecord) -> None:
            records.append(record)

        async def close(self) -> None:
            pass

    async with (
        trace_session(Sink(), organization_id=uuid4(), orchestration_run_id=uuid4()),
        trace_span("agent", agent_id="retrieve", agent_version="1.0.0") as span,
    ):
        span.capture_inputs(
            {"agent_id": "retrieve", "query": "private search", "url": "https://example.org"}
        )
        span.capture_outputs({"status": "COMPLETED", "excerpt": "private body"})
    agent = next(record for record in records if record.agent_id == "retrieve")
    assert agent.input_data == {"agent_id": "retrieve"}
    assert agent.output_data == {"status": "COMPLETED"}
