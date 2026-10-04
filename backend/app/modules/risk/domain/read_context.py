"""Permission-scoped read snapshot independent of the Agent runtime."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from app.modules.risk.domain.assessments import Contract, RiskFact


class ReadObservation(Contract):
    id: str
    text: str
    source_ids: tuple[str, ...]


class RiskReadContext(Contract):
    task_id: UUID
    task_version: int
    risk_assessment_id: UUID | None
    version: int = 1
    fingerprint: str
    state: Literal["READY", "PENDING", "STALE", "UNAVAILABLE"]
    score: str | None
    band: Literal["LOW", "MEDIUM", "HIGH"] | None
    scope: Literal["MANAGER", "OWN_WORK"]
    permitted_sources: tuple[RiskFact, ...] = Field(max_length=100)
    observations: tuple[ReadObservation, ...]
    rationale: str
    limitations: tuple[str, ...]
    recommendations: tuple[str, ...]
    affected_week_ids: tuple[UUID, ...]
