"""Current-context read and exact fingerprint revalidation."""

from pydantic import Field

from work_management_ai.agents.risk.contracts import Contract, RiskExplanationInput


class RiskToolInput(Contract):
    task_reference: str = Field(min_length=1, max_length=300)
    expected_fingerprint: str | None = None


class RiskToolOutput(RiskExplanationInput):
    pass
