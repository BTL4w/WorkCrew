"""AI owns the risk score; code validates references and threshold policy."""

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.people_capacity.application.workload_service import (
    RemainingWorkInput,
    remaining_work,
)
from app.modules.risk.domain.assessments import (
    RiskFact,
    RiskInputs,
    RiskJudgment,
    RiskObservation,
    risk_band,
    validate_risk_judgment,
)


def context():
    return RiskInputs(
        task_id=uuid4(),
        task_version=1,
        facts=(RiskFact(id="task:1", kind="TASK", values={"overdue": True}),),
        missing=(),
    )


def test_ai_risk_score_preserved_and_notification_permission():
    judgment = RiskJudgment(
        score=Decimal("83"),
        rationale="Deadline and blocker need review.",
        observations=(RiskObservation(text="Deadline risk", source_ids=("task:1",)),),
        recommendations=("Discuss the deadline.",),
    )
    result = validate_risk_judgment(context(), judgment)
    assert result.score == 83 and risk_band(result.score) == "HIGH"


@pytest.mark.parametrize(
    ("score", "band"),
    [("29.99", "LOW"), ("30", "MEDIUM"), ("59.99", "MEDIUM"), ("60", "HIGH"), ("100", "HIGH")],
)
def test_threshold_boundaries(score: str, band: str):
    assert risk_band(Decimal(score)) == band
    assert risk_band(None) is None


@pytest.mark.parametrize("score", ["-1", "101", "NaN", "Infinity"])
def test_invalid_score(score: str):
    with pytest.raises(ValidationError):
        RiskJudgment.model_validate({"score": score, "rationale": "Assessment", "observations": []})


def test_unknown_source_and_output_authority_rejected():
    judgment = RiskJudgment.model_validate(
        {
            "score": "83",
            "rationale": "Assessment",
            "observations": [{"text": "Unknown", "source_ids": ["foreign"]}],
        }
    )
    with pytest.raises(ValueError):
        validate_risk_judgment(context(), judgment)
    with pytest.raises(ValidationError):
        RiskJudgment.model_validate(
            {"score": "83", "rationale": "Assessment", "observations": [], "approved": True}
        )


@pytest.mark.parametrize("locale", ["vi", "en"])
def test_bilingual_judgment(locale: str):
    rationale = "Cần theo dõi hạn giao." if locale == "vi" else "Monitor the deadline."
    result = validate_risk_judgment(
        context(),
        RiskJudgment.model_validate(
            {
                "score": "37",
                "rationale": rationale,
                "observations": [{"text": rationale, "source_ids": ["task:1"]}],
            }
        ),
    )
    assert result.score == 37 and result.rationale == rationale


def test_remaining_work_unknown_known_overload_zero_and_prorating():
    result = remaining_work(
        RemainingWorkInput(
            weekly_capacity=Decimal("40"),
            leave_hours=Decimal("10"),
            remaining_days=2,
            efforts=(Decimal("8"), Decimal("7"), None),
        )
    )
    assert (
        result.available_hours == 12
        and result.known_hours == 15
        and result.overload is True
        and result.unknown_count == 1
    )
    assert (
        remaining_work(
            RemainingWorkInput(
                weekly_capacity=Decimal("40"),
                leave_hours=Decimal(0),
                remaining_days=5,
                efforts=(Decimal(8), None),
            )
        ).overload
        is None
    )
    assert (
        remaining_work(
            RemainingWorkInput(
                weekly_capacity=Decimal(0),
                leave_hours=Decimal(0),
                remaining_days=5,
                efforts=(Decimal(1),),
            )
        ).overload
        is True
    )


def test_weekly_risk_empty_unavailable_partial_and_maximum():
    from datetime import UTC, datetime

    from app.modules.risk.domain.assessments import RiskAssessment, weekly_risk

    week, task, unknown = uuid4(), uuid4(), uuid4()
    empty = weekly_risk(week, (), ())
    assert empty.state == "UNAVAILABLE" and empty.score is None and empty.task_count == 0
    result = RiskAssessment(
        id=uuid4(),
        task_id=task,
        task_version=1,
        state="READY",
        evaluated_at=datetime.now(UTC),
        judgment=RiskJudgment.model_validate({"score": "83", "rationale": "Needs review"}),
        band="HIGH",
    )
    projection = weekly_risk(week, (task, unknown), (result,))
    assert projection.state == "PARTIAL" and projection.score == 83
    assert projection.assessed_count == 1 and projection.unavailable_task_ids == (unknown,)


def test_notice_dedup_ignores_daily_age_but_tracks_real_context_changes():
    from app.modules.risk.domain.notifications import notification_state

    before = context()
    changed = before.model_copy(
        update={
            "facts": (
                before.facts[0].model_copy(
                    update={"values": {"overdue": True, "evaluation_date": "2026-10-04"}}
                ),
            )
        }
    )
    assert notification_state(before, "HIGH") == notification_state(changed, "HIGH")
    assert notification_state(before, "HIGH") != notification_state(before, "MEDIUM")


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["vi", "en"])
async def test_gateway_bilingual_structured_judgment_and_source_validation(locale: str):
    from app.modules.risk.adapters.model_assessment import GatewayRiskAssessment
    from work_management_ai.model_gateway.mock import MockModelGateway

    inputs = context()
    rationale = "Cần kiểm tra hạn hoàn thành." if locale == "vi" else "Review the deadline."
    gateway = MockModelGateway(
        fixtures={
            "risk.assess": {
                "score": "83",
                "rationale": rationale,
                "observations": [{"text": rationale, "source_ids": [inputs.facts[0].id]}],
                "limitations": ["Capacity unknown"],
                "recommendations": ["Discuss the date"],
            }
        }
    )
    result, model_ref = await GatewayRiskAssessment(gateway, locale).assess(inputs)
    assert result.score == 83 and result.rationale == rationale and model_ref.startswith("mock:")
    invalid = MockModelGateway(
        fixtures={
            "risk.assess": {
                "score": "83",
                "rationale": "Invalid source",
                "observations": [{"text": "Unauthorized", "source_ids": ["foreign"]}],
            }
        }
    )
    with pytest.raises(ValueError):
        await GatewayRiskAssessment(invalid).assess(inputs)


@pytest.mark.parametrize(
    "payload",
    [{}, {"rationale": "Assessment"}, {"score": "83"}, {"score": "83", "rationale": "   "}],
)
def test_missing_or_blank_required_model_fields_fail_closed(payload: dict[str, str]):
    with pytest.raises(ValidationError):
        RiskJudgment.model_validate(payload)


def test_risk_tool_schema_uses_portable_decimal_pattern_without_lookaround():
    import json
    import re

    from langchain_core.utils.function_calling import convert_to_openai_tool

    schema = convert_to_openai_tool(RiskJudgment)["function"]["parameters"]
    serialized = json.dumps(schema)
    assert "(?=" not in serialized and "(?!" not in serialized
    score = schema["properties"]["score"]
    patterns = [item["pattern"] for item in score["anyOf"] if "pattern" in item]
    assert patterns
    for value in ("0", "29.99", "30", "59.99", "60", "83", "100", "100.0"):
        assert re.fullmatch(patterns[0], value)
    for value in ("-1", "101", "NaN", "Infinity"):
        assert not re.fullmatch(patterns[0], value)
