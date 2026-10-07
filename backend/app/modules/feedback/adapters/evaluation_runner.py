"""Dataset-to-AI translation, current authorization and explicit hosted budget boundary."""

import json
from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, JsonValue

from app.core.config import Settings
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.planning_runs.adapters.ai_runtime import build_model_gateway
from app.modules.reporting.domain.reports import ReportError
from work_management_ai.agents.reporting.contracts import ReportingContext, ReportingMetric
from work_management_ai.evaluation.phase5_cases import ReportingEvalCase
from work_management_ai.evaluation.phase5_reporting import (
    MockReportingEvaluationGateway,
    ReportingEvaluationGateway,
    run_reporting_suite,
)
from work_management_ai.model_gateway.contracts import (
    ModelGateway,
    StructuredModelRequest,
    StructuredModelResponse,
)

from ..application.ports import EvaluationDatasetReadPort, EvaluationProviderPort
from ..domain.evaluation import EvaluationDatasetVersion, ReportEvaluationResult
from .transaction import CurationTransactions


class FrozenDatasetReader:
    def __init__(self, transactions: CurationTransactions):
        self.transactions = transactions

    async def load(self, actor: AuthenticatedActor, identity: UUID) -> EvaluationDatasetVersion:
        async with self.transactions(actor) as repo:
            await repo.authenticate()
            return await repo.frozen(identity)


def hosted_policy(settings: Settings, *, budget_tokens: int | None) -> None:
    policy = settings.report_evaluation_hosted_enabled
    if not policy:
        raise ReportError("EVALUATION_HOSTED_DISABLED", 403)
    if (
        settings.ai_provider != "openai"
        or settings.openai_api_key is None
        or not settings.openai_api_key.get_secret_value().strip()
        or not settings.ai_model.strip()
        or budget_tokens is None
        or not 1 <= budget_tokens <= 1_000_000
    ):
        raise ReportError("EVALUATION_HOSTED_CONFIGURATION_REQUIRED", 422)


class BudgetedGateway:
    def __init__(self, gateway: ModelGateway, *, budget_tokens: int):
        if not 1 <= budget_tokens <= 1_000_000:
            raise ValueError("INVALID_EVALUATION_BUDGET")
        self.gateway, self.budget = gateway, budget_tokens
        self.reserved = 0

    async def generate_structured[T: BaseModel](
        self, request: StructuredModelRequest[T]
    ) -> StructuredModelResponse[T]:
        inputs = (
            sum(len(message.content.encode()) for message in request.messages)
            + len(json.dumps(request.output_schema.model_json_schema()).encode())
            + 1024
        )
        reserved = inputs + (request.max_output_tokens or 3000)
        if self.reserved + reserved > self.budget:
            raise ValueError("EVALUATION_BUDGET_EXHAUSTED")
        self.reserved += reserved
        result = await self.gateway.generate_structured(request)
        if result.usage is not None and result.usage.total_tokens > reserved:
            raise ValueError("EVALUATION_USAGE_EXCEEDS_BUDGET")
        return result


class AuthorizedGateway:
    def __init__(self, gateway: ModelGateway, authorize: Callable[[], Awaitable[None]]):
        self.gateway, self.authorize = gateway, authorize

    async def generate_structured[T: BaseModel](
        self, request: StructuredModelRequest[T]
    ) -> StructuredModelResponse[T]:
        await self.authorize()
        result = await self.gateway.generate_structured(request)
        await self.authorize()
        return result


class HostedReportingEvaluationGateway:
    provider = "hosted"

    def __init__(
        self,
        settings: Settings,
        *,
        budget_tokens: int,
        authorize: Callable[[], Awaitable[None]],
        judge: bool = False,
    ):
        hosted_policy(settings, budget_tokens=budget_tokens)
        self.gateway = AuthorizedGateway(
            BudgetedGateway(build_model_gateway(settings), budget_tokens=budget_tokens), authorize
        )
        self.qualitative_judging = judge

    def for_case(
        self,
        case: ReportingEvalCase,
        context: ReportingContext,
        document: dict[str, Any],
        semantic: dict[str, Any],
    ) -> ModelGateway:
        return self.gateway


class ReportingEvaluationProvider:
    def __init__(self, gateway: ReportingEvaluationGateway | None = None):
        self.gateway = gateway or MockReportingEvaluationGateway()

    async def evaluate(self, dataset: EvaluationDatasetVersion) -> ReportEvaluationResult:
        if not dataset.verified_hash():
            raise ReportError("EVALUATION_INTEGRITY_FAILED")
        cases = tuple(
            ReportingEvalCase(
                id=f"{case.id}:v{case.version}",
                locale=case.payload.locale,
                scenario="valid",
                expected="PROPOSAL",
                origin=case.origin,
                period_kind=case.payload.period_kind,
                metrics={
                    key: ReportingMetric.model_validate(metric.model_dump(mode="json"))
                    for key, metric in case.payload.metrics.items()
                },
                expected_metric_keys=tuple(
                    assertion.metric_key for assertion in case.expected_assertions
                ),
                provenance={
                    "dataset_version_id": str(dataset.id),
                    "case_id": str(case.id),
                    "case_version": case.version,
                    "case_hash": case.case_hash,
                    "split": case.split,
                    "origin": case.origin,
                    "policy_version": case.policy_version,
                    "source": case.provenance,
                },
            )
            for case in dataset.cases
        )
        result = await run_reporting_suite(cases, self.gateway, dataset_hash=dataset.dataset_hash)
        return ReportEvaluationResult(
            dataset_version_id=dataset.id,
            dataset_hash=dataset.dataset_hash,
            provider=cast(Literal["mock", "hosted"], self.gateway.provider),
            total=result.total,
            passed=result.passed,
            failed=result.failed,
            skipped=result.skipped,
            gate_passed=result.gate.passed,
            gate=cast(dict[str, JsonValue], result.gate.model_dump(mode="json")),
            cases=tuple(
                cast(dict[str, JsonValue], item.model_dump(mode="json")) for item in result.results
            ),
            hosted_quality=result.hosted_quality,
            limitations=(
                *result.limitations,
                "METRIC_ONLY_DATASET_MAY_NOT_EXERCISE_ALL_REQUIRED_POLICY_DIMENSIONS",
            ),
        )


class EvaluationRunner:
    def __init__(
        self, reader: EvaluationDatasetReadPort, provider: EvaluationProviderPort | None = None
    ):
        self.reader, self.provider = reader, provider or ReportingEvaluationProvider()

    async def run(
        self, *, actor: AuthenticatedActor, dataset_version_id: UUID
    ) -> ReportEvaluationResult:
        dataset = await self.reader.load(actor, dataset_version_id)
        result = await self.provider.evaluate(dataset)
        # Recheck current authorization before delivering results.
        await self.reader.load(actor, dataset_version_id)
        return result
