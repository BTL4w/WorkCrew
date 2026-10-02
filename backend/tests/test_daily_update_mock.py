import pytest

from app.core.config import Settings
from app.modules.planning_runs.adapters.ai_runtime import build_model_gateway
from work_management_ai.agents.daily_update.contracts import ExtractedReport
from work_management_ai.model_gateway.contracts import ModelMessage, StructuredModelRequest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "locale,text", [("en", "Completed survey; progress 50%."), ("vi", "Đã khảo sát, tiến độ 50%.")]
)
async def test_local_mock_can_demo_owner_draft(locale: str, text: str):
    import json

    response = await build_model_gateway(Settings(ai_provider="mock")).generate_structured(
        StructuredModelRequest(
            invocation_key=f"daily_update.{locale}.extract",
            messages=(ModelMessage(role="user", content=json.dumps({"report": text})),),
            output_schema=ExtractedReport,
            timeout_seconds=60,
            max_output_tokens=1500,
        )
    )
    assert response.parsed.reported_percent == 50
    assert response.parsed.done_text == text
