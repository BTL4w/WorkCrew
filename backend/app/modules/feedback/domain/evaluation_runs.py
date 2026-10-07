"""Admin-only immutable run identity and safe measured results."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.modules.reporting.domain.reports import ReportContract

from .evaluation import ReportEvaluationResult

PROVIDER_POLICY = "report-eval-provider.v1"


class EvaluationRequest(ReportContract):
    dataset_version_id: UUID
    provider: Literal["mock", "hosted"] = "mock"


class EvaluationRun(ReportContract):
    id: UUID
    organization_id: UUID
    requester_membership_id: UUID
    dataset_version_id: UUID
    dataset_version: int
    dataset_hash: str
    dataset_policy_version: str
    provider: Literal["mock", "hosted"]
    provider_policy_version: Literal["report-eval-provider.v1"] = PROVIDER_POLICY
    provider_config_hash: str
    budget_tokens: int = Field(ge=1, le=1_000_000)
    status: Literal["QUEUED", "RUNNING", "PASSED", "FAILED", "CANCELLED"]
    failure_kind: Literal["GATE", "WORKER", "AUTHORIZATION", "POLICY"] | None = None
    safe_error_code: str | None = None
    fence: int = 0
    attempts: int = 0
    lease_owner: str | None = None
    lease_until: datetime | None = None
    created_at: datetime
    result: ReportEvaluationResult | None = None
    replayed: bool = False
