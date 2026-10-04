"""Read-only explanation contracts never accept a replacement score."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from work_management_ai.runtime.contracts import JsonScalar, JsonValue


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskQuestion(Contract):
    task_reference: str = Field(min_length=1, max_length=300)
    locale: Literal["vi", "en"]
    question: str = Field(min_length=1, max_length=8000)


class PermittedSource(Contract):
    id: str
    kind: str
    values: dict[str, JsonValue]


class StoredObservation(Contract):
    id: str
    text: str
    source_ids: tuple[str, ...]


class RiskExplanationInput(Contract):
    task_id: UUID
    task_version: int = Field(ge=1)
    risk_assessment_id: UUID | None
    version: int = Field(ge=1)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: Literal["READY", "PENDING", "STALE", "UNAVAILABLE"]
    score: str | None
    band: Literal["LOW", "MEDIUM", "HIGH"] | None
    scope: Literal["MANAGER", "OWN_WORK"]
    permitted_sources: tuple[PermittedSource, ...] = Field(max_length=100)
    observations: tuple[StoredObservation, ...] = Field(max_length=100)
    rationale: str
    limitations: tuple[str, ...]
    recommendations: tuple[str, ...]
    affected_week_ids: tuple[UUID, ...]


class RiskAssertion(Contract):
    source_id: str
    field: str
    value: JsonScalar


class ObservationExplanation(Contract):
    text: str = Field(min_length=1, max_length=2000)
    observation_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    source_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    assertions: tuple[RiskAssertion, ...] = Field(max_length=20)


class RiskExplanation(Contract):
    observation_explanations: tuple[ObservationExplanation, ...] = Field(max_length=10)
    limitations: tuple[str, ...] = Field(max_length=10)
    recommendations: tuple[str, ...] = Field(max_length=5)
    replan_requested: bool


class RiskReplanRequest(Contract):
    affected_week_ids: tuple[UUID, ...]
    observation_ids: tuple[str, ...]
    risk_assessment_id: UUID | None
    task_id: UUID


class RiskCardContent(RiskExplanationInput):
    explanation: RiskExplanation
    fallback: bool
