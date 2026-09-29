"""Provider-neutral Model Gateway contract tests."""

from copy import deepcopy
from datetime import date
from typing import cast

import pytest

from work_management_ai.model_gateway.contracts import ModelMessage, StructuredModelRequest
from work_management_ai.model_gateway.errors import (
    ModelInvalidOutputError,
    ModelRateLimitError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from work_management_ai.model_gateway.mock import MockModelGateway
from work_management_ai.schemas.planning import PlanningModelOutput

VALID_PLAN: dict[str, object] = {
    "project": {
        "title": "Kế hoạch xuất nhập khẩu",
        "description": "Chuẩn bị lô hàng quý IV",
        "start_date": "2026-08-10",
        "due_date": "2026-09-30",
    },
    "goal": {
        "title": "Hoàn tất lô hàng đúng hạn",
        "description": None,
        "expected_outcomes": ["Hàng được thông quan trước ngày khởi hành"],
        "target_date": "2026-09-20",
    },
    "milestones": [
        {
            "ref": "milestone.customs",
            "title": "Hoàn tất thủ tục hải quan",
            "description": None,
            "due_date": "2026-09-10",
        }
    ],
    "project_weeks": [
        {
            "ref": "week.customs",
            "week_number": 1,
            "start_date": "2026-08-10",
            "end_date": "2026-09-01",
            "objective": "Chuẩn bị và duyệt chứng từ",
        }
    ],
    "tasks": [
        {
            "ref": "task.documents",
            "project_week_ref": "week.customs",
            "milestone_ref": "milestone.customs",
            "title": "Chuẩn bị chứng từ",
            "description": None,
            "due_date": "2026-09-01",
            "assignee_membership_id": None,
            "required_skill_labels": ["chứng từ xuất nhập khẩu"],
            "estimated_effort_hours": 24,
            "acceptance_criteria": ["Bộ chứng từ được quản lý phê duyệt"],
        }
    ],
    "dependencies": [],
    "assumptions": [
        {
            "description": "Lịch tàu không thay đổi",
            "source": "manager_input",
        }
    ],
}


def request(invocation_key: str) -> StructuredModelRequest[PlanningModelOutput]:
    """Build the same provider-neutral request for each observable outcome."""

    return StructuredModelRequest(
        invocation_key=invocation_key,
        messages=(ModelMessage(role="user", content="Lập kế hoạch xuất nhập khẩu"),),
        output_schema=PlanningModelOutput,
        timeout_seconds=60,
    )


@pytest.mark.asyncio
async def test_mock_gateway_returns_typed_output_and_version_metadata() -> None:
    gateway = MockModelGateway(fixtures={"planning.default.vi.v1": VALID_PLAN})

    response = await gateway.generate_structured(request("planning.default.vi.v1"))

    assert isinstance(response.parsed, PlanningModelOutput)
    assert response.parsed.project.start_date == date(2026, 8, 10)
    assert response.parsed.goal.expected_outcomes == ["Hàng được thông quan trước ngày khởi hành"]
    assert response.parsed.goal.target_date == date(2026, 9, 20)
    assert response.model_ref == "mock:planning-v1"
    assert response.usage is None
    assert request("planning.default.vi.v1").messages[0].images == ()


@pytest.mark.asyncio
async def test_mock_gateway_copies_nested_fixture_values() -> None:
    mutable_plan = deepcopy(VALID_PLAN)
    gateway = MockModelGateway(fixtures={"planning.default.vi.v1": mutable_plan})
    project = cast(dict[str, object], mutable_plan["project"])
    project["title"] = "Changed after gateway construction"

    response = await gateway.generate_structured(request("planning.default.vi.v1"))

    assert response.parsed.project.title == "Kế hoạch xuất nhập khẩu"


@pytest.mark.asyncio
async def test_mock_gateway_normalizes_timeout() -> None:
    gateway = MockModelGateway(fixtures={"planning.timeout": TimeoutError("provider timed out")})

    with pytest.raises(ModelTimeoutError):
        await gateway.generate_structured(request("planning.timeout"))


@pytest.mark.asyncio
async def test_mock_gateway_normalizes_unavailable_provider() -> None:
    gateway = MockModelGateway(
        fixtures={"planning.unavailable": ConnectionError("provider unavailable")}
    )

    with pytest.raises(ModelUnavailableError):
        await gateway.generate_structured(request("planning.unavailable"))


@pytest.mark.asyncio
async def test_mock_gateway_normalizes_rate_limit() -> None:
    gateway = MockModelGateway(
        fixtures={"planning.rate_limit": ModelRateLimitError("rate limited")}
    )

    with pytest.raises(ModelRateLimitError):
        await gateway.generate_structured(request("planning.rate_limit"))


@pytest.mark.asyncio
async def test_mock_gateway_rejects_invalid_structured_output() -> None:
    gateway = MockModelGateway(fixtures={"planning.invalid": {"project": {}}})

    with pytest.raises(ModelInvalidOutputError):
        await gateway.generate_structured(request("planning.invalid"))


@pytest.mark.asyncio
async def test_planning_output_rejects_assignee_and_accepts_null_contract() -> None:
    plan_with_assignee = deepcopy(VALID_PLAN)
    tasks = cast(list[object], plan_with_assignee["tasks"])
    first_task = cast(dict[str, object], tasks[0])
    first_task["assignee_membership_id"] = "be338caf-6d7c-48c7-85c8-cbb7d4e2c841"
    gateway = MockModelGateway(fixtures={"planning.with-assignee": plan_with_assignee})

    with pytest.raises(ModelInvalidOutputError):
        await gateway.generate_structured(request("planning.with-assignee"))
    without_assignee = await MockModelGateway(
        fixtures={"planning.without-assignee": VALID_PLAN}
    ).generate_structured(request("planning.without-assignee"))

    assert without_assignee.parsed.tasks[0].assignee_membership_id is None


@pytest.mark.asyncio
async def test_mock_gateway_reports_missing_fixture_as_unavailable() -> None:
    gateway = MockModelGateway(fixtures={})

    with pytest.raises(ModelUnavailableError):
        await gateway.generate_structured(request("planning.missing"))
