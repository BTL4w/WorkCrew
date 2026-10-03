"""One bounded structured call through the provider-neutral Model Gateway."""

import json

from app.modules.risk.domain.assessments import RiskInputs, RiskJudgment, validate_risk_judgment
from work_management_ai.model_gateway.contracts import (
    ModelGateway,
    ModelMessage,
    StructuredModelRequest,
)
from work_management_ai.prompts.risk_assessment_v1 import RISK_ASSESSMENT_V1


class GatewayRiskAssessment:
    def __init__(self, gateway: ModelGateway, locale: str = "vi"):
        self.gateway = gateway
        self.locale = locale

    async def assess(self, inputs: RiskInputs) -> tuple[RiskJudgment, str]:
        content = json.dumps(
            {"locale": self.locale, "inputs": inputs.model_dump(mode="json")}, ensure_ascii=False
        )
        # UTF-8 bytes bound text tokens; no original media in this call.
        if len(content.encode()) > 12000:
            raise ValueError("RISK_CONTEXT_BUDGET")
        response = await self.gateway.generate_structured(
            StructuredModelRequest(
                invocation_key="risk.assess",
                messages=(
                    ModelMessage(role="system", content=RISK_ASSESSMENT_V1),
                    ModelMessage(role="user", content=content),
                ),
                output_schema=RiskJudgment,
                timeout_seconds=20,
                max_output_tokens=1500,
            )
        )
        return validate_risk_judgment(inputs, response.parsed), response.model_ref
