"""Typed contextual judgments; no factor points or model-granted authority."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Text = Annotated[str, Field(min_length=1, max_length=2000)]


class RiskFact(Contract):
    id: str
    kind: Literal["TASK", "PROGRESS", "BLOCKER", "DEPENDENCY", "CAPACITY", "WARNING", "BASELINE"]
    values: dict[str, JsonValue]


class RiskInputs(Contract):
    task_id: UUID
    task_version: int
    facts: tuple[RiskFact, ...] = Field(max_length=100)
    missing: tuple[str, ...] = ()


class RiskObservation(Contract):
    text: Text
    source_ids: tuple[str, ...] = Field(min_length=1, max_length=20)


class RiskJudgment(Contract):
    # Decimal's generated validation regex uses lookaround, which is not portable
    # to structured-output providers. Emit exact decimal strings with a plain
    # range pattern; the Decimal validators still enforce range/finite values.
    score: Decimal | None = Field(
        ge=0,
        le=100,
        allow_inf_nan=False,
        json_schema_extra={
            "anyOf": [
                {
                    "type": "string",
                    "pattern": r"^(?:100(?:\.0+)?|(?:[0-9]|[1-9][0-9])(?:\.[0-9]+)?)$",
                },
                {"type": "null"},
            ]
        },
    )
    rationale: Text
    observations: tuple[RiskObservation, ...] = Field(default=(), max_length=10)
    limitations: tuple[Text, ...] = Field(default=(), max_length=10)
    recommendations: tuple[Text, ...] = Field(default=(), max_length=5)

    @field_validator("rationale")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Rationale required")
        return value.strip()


def validate_risk_judgment(inputs: RiskInputs, judgment: RiskJudgment) -> RiskJudgment:
    judgment = RiskJudgment.model_validate(judgment.model_dump())
    sources = {f.id for f in inputs.facts}
    if len(sources) != len(inputs.facts):
        raise ValueError("Duplicate sources")
    if judgment.score is not None and not judgment.observations:
        raise ValueError("Score needs sources")
    if any(not set(o.source_ids).issubset(sources) for o in judgment.observations):
        raise ValueError("Unauthorized sources")
    return judgment


def risk_band(score: Decimal | None) -> Literal["LOW", "MEDIUM", "HIGH"] | None:
    if score is None:
        return None
    if not score.is_finite() or not 0 <= score <= 100:
        raise ValueError("Invalid score")
    return "LOW" if score < 30 else "MEDIUM" if score < 60 else "HIGH"


class RiskAssessment(Contract):
    id: UUID
    task_id: UUID
    task_version: int
    state: Literal["PENDING", "READY", "UNAVAILABLE", "STALE"]
    evaluated_at: datetime
    input_snapshot: RiskInputs | None = None
    judgment: RiskJudgment | None = None
    band: Literal["LOW", "MEDIUM", "HIGH"] | None = None
    limitation: str = ""
    policy_version: Literal["risk.ai.v1"] = "risk.ai.v1"
    model_ref: str | None = None
    prompt_version: Literal["risk-assessment.v1"] = "risk-assessment.v1"
    schema_version: Literal["risk-judgment.v1"] = "risk-judgment.v1"


class RiskReviewCommand(Contract):
    disposition: Literal["NEEDS_FOLLOWUP", "ACCEPTED_EXPLANATION", "RESOLVED"]
    reason: Text

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Review reason required")
        return value.strip()


class RiskReviewEvent(RiskReviewCommand):
    id: UUID
    risk_id: UUID
    actor_membership_id: UUID
    at: datetime


class WeeklyRisk(Contract):
    project_week_id: UUID
    state: Literal["READY", "PARTIAL", "UNAVAILABLE"]
    score: Decimal | None
    band: Literal["LOW", "MEDIUM", "HIGH"] | None
    task_count: int
    assessed_count: int
    unavailable_task_ids: tuple[UUID, ...]


def weekly_risk(
    week_id: UUID, tasks: tuple[UUID, ...], results: tuple[RiskAssessment, ...]
) -> WeeklyRisk:
    known = {
        r.task_id: r.judgment.score
        for r in results
        if r.task_id in tasks and r.state == "READY" and r.judgment and r.judgment.score is not None
    }
    score = max(known.values()) if known else None
    missing = tuple(t for t in tasks if t not in known)
    return WeeklyRisk(
        project_week_id=week_id,
        state="UNAVAILABLE" if score is None else "PARTIAL" if missing else "READY",
        score=score,
        band=risk_band(score),
        task_count=len(tasks),
        assessed_count=len(known),
        unavailable_task_ids=missing,
    )
