"""Strict manifest schemas and safe package-resource loading."""

import hashlib
import importlib
import json
import re
from importlib.resources import files
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from work_management_ai.runtime.contracts import AgentId, RiskLevel

_SEMANTIC_VERSION = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_VERSIONED_REFERENCE = re.compile(r"^[a-z][a-z0-9_.-]*@(0|[1-9]\d*)$")
_CONTRACT_PATH = re.compile(
    r"^work_management_ai(?:\.[A-Za-z_][A-Za-z0-9_]*)+\.[A-Za-z_][A-Za-z0-9_]*$"
)


class _ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _semantic_version(value: str) -> str:
    if _SEMANTIC_VERSION.fullmatch(value) is None:
        raise ValueError("version must be an exact semantic version")
    return value


def _contract_path(value: str) -> str:
    if _CONTRACT_PATH.fullmatch(value) is None:
        raise ValueError("contract path must be inside work_management_ai")
    return value


def _versioned_reference(value: str) -> str:
    if _VERSIONED_REFERENCE.fullmatch(value) is None:
        raise ValueError("reference must use name@major")
    return value


class AgentMetadata(_ManifestModel):
    id: AgentId
    name: str = Field(min_length=1, max_length=100)
    version: str
    owner: str = Field(min_length=1, max_length=100)
    activation_phase: int = Field(ge=2, le=99)

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return _semantic_version(value)


class AgentContractManifest(_ManifestModel):
    input: str
    output: str
    handoff: str

    @field_validator("input", "output", "handoff")
    @classmethod
    def validate_contract_paths(cls, value: str) -> str:
        return _contract_path(value)


class AgentPermissionManifest(_ManifestModel):
    roles: tuple[Literal["ADMIN", "MANAGER", "EMPLOYEE"], ...] = Field(min_length=1)
    tenant_scope: Literal["actor_membership"]
    risk_ceiling: RiskLevel


class AgentRuntimeManifest(_ManifestModel):
    max_model_attempts: int = Field(default=3, ge=0, le=3)
    max_input_tokens: int = Field(default=24000, ge=0, le=48000)
    max_output_tokens: int = Field(default=4000, ge=0, le=8000)
    workflow: str = Field(min_length=1, max_length=100)
    max_iterations: int = Field(ge=1, le=16)
    max_tool_calls: int = Field(ge=0, le=32)
    max_handoffs: int = Field(default=0, ge=0, le=16)
    max_replans: int = Field(default=0, ge=0, le=4)
    timeout_seconds: int = Field(ge=1, le=180)
    checkpoint: Literal["durable", "ephemeral"]
    model_policy: str = Field(min_length=1, max_length=100)


class AgentApprovalManifest(_ManifestModel):
    produced_writes: Literal["NEVER", "ALWAYS", "POLICY"]
    can_self_approve: Literal[False]


class AgentFallbackManifest(_ManifestModel):
    strategy: str = Field(min_length=1, max_length=100)


class AgentManifest(_ManifestModel):
    schema_version: Literal["1.0"]
    agent: AgentMetadata
    capabilities: tuple[str, ...] = Field(min_length=1)
    contracts: AgentContractManifest
    permissions: AgentPermissionManifest
    runtime: AgentRuntimeManifest
    allowed_skills: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    approval: AgentApprovalManifest
    fallback: AgentFallbackManifest
    evaluators: tuple[str, ...] = Field(min_length=1)
    stop_conditions: tuple[str, ...] = ()

    @field_validator("allowed_skills", "allowed_tools", "evaluators")
    @classmethod
    def validate_versioned_references(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_versioned_reference(value) for value in values)

    @model_validator(mode="after")
    def validate_unique_declarations(self) -> "AgentManifest":
        declarations = (
            self.capabilities,
            self.allowed_skills,
            self.allowed_tools,
            self.evaluators,
            self.permissions.roles,
            self.stop_conditions,
        )
        if any(len(values) != len(set(values)) for values in declarations):
            raise ValueError("manifest declarations must be unique")
        return self


class SkillManifest(_ManifestModel):
    schema_version: Literal["1.0"]
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=100)
    version: str
    owner: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    runnable_by_agents: tuple[AgentId, ...] = Field(min_length=1)
    allowed_tools: tuple[str, ...] = ()
    risk_level: RiskLevel
    input_contract: str
    output_contract: str
    evaluators: tuple[str, ...] = Field(min_length=1)
    evaluation_cases: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()
    required_context: tuple[str, ...] = ()
    approval: Literal["NONE", "ALWAYS", "POLICY"] = "NONE"
    stop_conditions: tuple[str, ...] = ()

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return _semantic_version(value)

    @field_validator("allowed_tools", "evaluators", "evaluation_cases")
    @classmethod
    def validate_versioned_references(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_versioned_reference(value) for value in values)

    @field_validator("input_contract", "output_contract")
    @classmethod
    def validate_contract_paths(cls, value: str) -> str:
        return _contract_path(value)

    @model_validator(mode="after")
    def validate_unique_declarations(self) -> "SkillManifest":
        declarations = (
            self.runnable_by_agents,
            self.allowed_tools,
            self.evaluators,
            self.evaluation_cases,
            self.triggers,
            self.required_context,
            self.stop_conditions,
        )
        if any(len(values) != len(set(values)) for values in declarations):
            raise ValueError("skill manifest declarations must be unique")
        return self


class ToolManifest(_ManifestModel):
    schema_version: Literal["1.0"]
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=100)
    version: str
    owner: str = Field(min_length=1, max_length=100)
    tenant_scope: Literal["actor_membership"]
    roles: tuple[Literal["ADMIN", "MANAGER", "EMPLOYEE"], ...] = ()
    risk_level: RiskLevel
    input_contract: str
    output_contract: str
    timeout_seconds: int = Field(ge=1, le=180)
    max_attempts: int = Field(ge=1, le=3)
    retry_policy: Literal["NONE", "IDEMPOTENT"] = "NONE"
    idempotency: Literal["NOT_APPLICABLE", "OPTIONAL", "REQUIRED"]
    audit: Literal["NONE", "SAFE_METADATA", "REQUIRED"]
    evidence_output: bool = False
    freshness_output: bool = False
    trace_metadata: tuple[
        Literal["tool_id", "duration_ms", "result_status", "evidence_count"], ...
    ] = ()

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return _semantic_version(value)

    @field_validator("input_contract", "output_contract")
    @classmethod
    def validate_contract_paths(cls, value: str) -> str:
        return _contract_path(value)


def load_yaml_resource[ManifestT: BaseModel](
    package: str, resource: str, model_type: type[ManifestT]
) -> ManifestT:
    """Load a package YAML resource without evaluating custom YAML tags."""

    loaded = yaml.safe_load(files(package).joinpath(resource).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("manifest root must be a mapping")
    return model_type.model_validate(loaded)


def canonical_manifest_fingerprint(manifest: BaseModel) -> str:
    payload = json.dumps(
        manifest.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def resolve_contract(path: str) -> Any:
    """Resolve a validated project-owned contract path."""

    _contract_path(path)
    module_name, _, attribute = path.rpartition(".")
    module = importlib.import_module(module_name)
    return getattr(module, attribute)
