"""Optional automation and report provenance remain additive typed public contracts."""

from app.main import app
from app.modules.automations.domain.schedules import ScheduleCommand


def test_summary_narrative_openapi_contract():
    schemas = app.openapi()["components"]["schemas"]
    command = schemas["ScheduleCommand"]["properties"]
    assert command["narrative_mode"]["enum"] == ["NONE", "DRAFT_FOR_MANAGER"]
    assert command["narrative_mode"]["default"] == "NONE"
    assert "creator_membership_id" not in ScheduleCommand.model_fields
    delivery = schemas["SummaryDelivery"]["properties"]
    assert "report_link" in delivery and "narrative" not in delivery
    assert schemas["Report"]["properties"]["origin"]["default"] == "ON_DEMAND"
