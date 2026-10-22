"""Validated exact-version Agent registry."""

from dataclasses import dataclass

from work_management_ai.runtime.activation import ActivationContext, capability_enabled
from work_management_ai.runtime.contracts import AgentId, RiskLevel
from work_management_ai.runtime.manifests import (
    AnyAgentManifest,
    CapabilityAgentManifest,
    canonical_manifest_fingerprint,
    load_agent_manifest_resource,
    resolve_contract,
)
from work_management_ai.runtime.skill_registry import (
    RegisteredSkill,
    SkillRegistry,
    SkillRegistryError,
)
from work_management_ai.runtime.tool_registry import (
    RegisteredTool,
    ToolRegistry,
    ToolRegistryError,
)


class AgentRegistryError(ValueError):
    pass


_RISK_ORDER = {
    RiskLevel.READ_ONLY: 0,
    RiskLevel.PROPOSAL_ONLY: 1,
    RiskLevel.EXPLICIT_WRITE: 2,
}


@dataclass(frozen=True, slots=True)
class RegisteredAgent:
    manifest: AnyAgentManifest
    fingerprint: str


class AgentRegistry:
    def __init__(
        self,
        *,
        skill_registry: SkillRegistry,
        tool_registry: ToolRegistry,
        evaluator_ids: frozenset[str],
    ) -> None:
        self._skill_registry = skill_registry
        self._tool_registry = tool_registry
        self._evaluator_ids = evaluator_ids
        self._entries: dict[tuple[AgentId, str], RegisteredAgent] = {}

    def register_resource(self, package: str, resource: str) -> RegisteredAgent:
        manifest = load_agent_manifest_resource(package, resource)
        for contract_path in (
            manifest.contracts.input,
            manifest.contracts.output,
            manifest.contracts.handoff,
        ):
            try:
                resolve_contract(contract_path)
            except (AttributeError, ImportError, ValueError) as exc:
                raise AgentRegistryError("AGENT_CONTRACT_INVALID") from exc
        skills: list[RegisteredSkill] = []
        for reference in manifest.allowed_skills:
            try:
                skill = self._skill_registry.resolve(reference)
            except SkillRegistryError as exc:
                raise AgentRegistryError("AGENT_SKILL_NOT_REGISTERED") from exc
            skills.append(skill)
        tools: list[RegisteredTool] = []
        for reference in manifest.allowed_tools:
            try:
                tool = self._tool_registry.resolve(reference)
            except ToolRegistryError as exc:
                raise AgentRegistryError("AGENT_TOOL_NOT_REGISTERED") from exc
            tools.append(tool)
        if any(reference not in self._evaluator_ids for reference in manifest.evaluators):
            raise AgentRegistryError("AGENT_EVALUATOR_NOT_REGISTERED")
        if (
            manifest.permissions.risk_ceiling is RiskLevel.READ_ONLY
            and manifest.approval.produced_writes != "NEVER"
        ):
            raise AgentRegistryError("AGENT_WRITE_POLICY_INCOMPATIBLE")

        for skill in skills:
            if manifest.agent.id not in skill.manifest.runnable_by_agents:
                raise AgentRegistryError("AGENT_SKILL_PERMISSION_INCOMPATIBLE")
            if (
                _RISK_ORDER[skill.manifest.risk_level]
                > _RISK_ORDER[manifest.permissions.risk_ceiling]
            ):
                raise AgentRegistryError("AGENT_SKILL_RISK_INCOMPATIBLE")
            if not set(skill.manifest.allowed_tools).issubset(manifest.allowed_tools):
                raise AgentRegistryError("AGENT_SKILL_TOOL_INCOMPATIBLE")
            if any(evaluator not in self._evaluator_ids for evaluator in skill.manifest.evaluators):
                raise AgentRegistryError("AGENT_SKILL_EVALUATOR_NOT_REGISTERED")
        for tool in tools:
            if (
                _RISK_ORDER[tool.manifest.risk_level]
                > _RISK_ORDER[manifest.permissions.risk_ceiling]
            ):
                raise AgentRegistryError("AGENT_TOOL_RISK_INCOMPATIBLE")
            if tool.manifest.roles and not set(manifest.permissions.roles).issubset(
                tool.manifest.roles
            ):
                raise AgentRegistryError("AGENT_TOOL_ROLE_INCOMPATIBLE")

        key = (manifest.agent.id, manifest.agent.version)
        registered = RegisteredAgent(
            manifest=manifest,
            fingerprint=canonical_manifest_fingerprint(manifest),
        )
        existing = self._entries.get(key)
        if existing is not None and existing != registered:
            raise AgentRegistryError("AGENT_VERSION_ALREADY_REGISTERED")
        self._entries[key] = registered
        return registered

    def resolve(
        self,
        agent_id: AgentId,
        version: str,
        active_phase: int,
        *,
        activation: ActivationContext | None = None,
    ) -> RegisteredAgent:
        registered = self._entries.get((agent_id, version))
        if registered is None:
            raise AgentRegistryError("AGENT_VERSION_NOT_REGISTERED")
        if registered.manifest.agent.activation_phase > active_phase:
            raise AgentRegistryError("AGENT_PHASE_INACTIVE")
        if isinstance(registered.manifest, CapabilityAgentManifest) and not all(
            capability_enabled(capability, activation)
            for capability in registered.manifest.capabilities
        ):
            raise AgentRegistryError("AGENT_CAPABILITY_INACTIVE")
        return registered

    def planning_catalog(
        self,
        *,
        active_phase: int,
        role: str,
        activation: ActivationContext | None = None,
    ) -> tuple[dict[str, object], ...]:
        """Return model context only; registry resolution remains authoritative."""
        catalog: list[dict[str, object]] = []
        for registered in self._entries.values():
            manifest = registered.manifest
            if (
                manifest.agent.id is AgentId.ORCHESTRATOR
                or manifest.agent.activation_phase > active_phase
                or role not in manifest.permissions.roles
            ):
                continue
            if isinstance(manifest, CapabilityAgentManifest) and not all(
                capability_enabled(capability, activation) for capability in manifest.capabilities
            ):
                continue
            catalog.append(
                {
                    "agent_id": manifest.agent.id.value,
                    "agent_version": manifest.agent.version,
                    "capabilities": list(manifest.capabilities),
                    "risk_ceiling": manifest.permissions.risk_ceiling.value,
                }
            )
        return tuple(sorted(catalog, key=lambda item: str(item["agent_id"])))
