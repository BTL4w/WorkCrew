from uuid import UUID, uuid4

import pytest

from app.core.config import Settings
from app.modules.feedback.adapters.evaluation_runner import EvaluationRunner, hosted_policy
from app.modules.feedback.domain.evaluation import EvaluationDatasetVersion
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.reporting.domain.reports import ReportError


def test_hosted_requires_enabled_policy_credentials_and_explicit_budget():
    with pytest.raises(ReportError, match="EVALUATION_HOSTED_DISABLED"):
        hosted_policy(Settings(environment="test"), budget_tokens=1000)
    with pytest.raises(ReportError):
        hosted_policy(
            Settings(environment="test", report_evaluation_hosted_enabled=True), budget_tokens=0
        )


@pytest.mark.asyncio
async def test_dataset_runner_checks_current_admin_before_provider_call():
    class Denied:
        async def load(self, actor: AuthenticatedActor, identity: UUID) -> EvaluationDatasetVersion:
            raise ReportError("FORBIDDEN", 403)

    runner = EvaluationRunner(Denied())
    with pytest.raises(ReportError, match="FORBIDDEN"):
        await runner.run(
            actor=AuthenticatedActor(
                uuid4(),
                "admin@example.test",
                "Admin",
                uuid4(),
                uuid4(),
                "Synthetic",
                MembershipRole.ADMIN,
            ),
            dataset_version_id=uuid4(),
        )


@pytest.mark.asyncio
async def test_explicit_hosted_budget_stops_before_transport():
    from pydantic import BaseModel

    from app.modules.feedback.adapters.evaluation_runner import BudgetedGateway
    from work_management_ai.model_gateway.contracts import (
        ModelMessage,
        StructuredModelRequest,
        StructuredModelResponse,
    )

    class Output(BaseModel):
        value: int

    class Never:
        async def generate_structured[T: BaseModel](
            self, request: StructuredModelRequest[T]
        ) -> StructuredModelResponse[T]:
            raise AssertionError("must not reach network")

    gateway = BudgetedGateway(Never(), budget_tokens=1)
    with pytest.raises(ValueError, match="EVALUATION_BUDGET_EXHAUSTED"):
        await gateway.generate_structured(
            StructuredModelRequest(
                invocation_key="test",
                messages=(ModelMessage(role="user", content="numeric fixture"),),
                output_schema=Output,
                timeout_seconds=1,
                max_output_tokens=100,
            )
        )


@pytest.mark.asyncio
async def test_hosted_authorization_is_rechecked_at_each_transport_boundary():
    from pydantic import BaseModel

    from app.modules.feedback.adapters.evaluation_runner import AuthorizedGateway
    from work_management_ai.model_gateway.contracts import (
        ModelMessage,
        StructuredModelRequest,
        StructuredModelResponse,
    )

    class Output(BaseModel):
        value: int

    calls: list[str] = []

    async def authorize() -> None:
        calls.append("authorize")
        if len(calls) > 2:
            raise ReportError("FORBIDDEN", 403)

    class Gateway:
        async def generate_structured[T: BaseModel](
            self, request: StructuredModelRequest[T]
        ) -> StructuredModelResponse[T]:
            calls.append("transport")
            return StructuredModelResponse(
                parsed=request.output_schema.model_validate({"value": 1}),
                model_ref="mock:transport",
            )

    gateway = AuthorizedGateway(Gateway(), authorize)
    with pytest.raises(ReportError, match="FORBIDDEN"):
        await gateway.generate_structured(
            StructuredModelRequest(
                invocation_key="test",
                messages=(ModelMessage(role="user", content="safe numeric"),),
                output_schema=Output,
                timeout_seconds=1,
            )
        )
    assert calls == ["authorize", "transport", "authorize"]
