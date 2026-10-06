# pyright: reportArgumentType=false
"""Stored cards and links never confer authority."""

from typing import Any
from uuid import uuid4

import pytest

from app.modules.assistant.adapters.reporting_tools import ReportBlockProjector


@pytest.mark.asyncio
async def test_employee_and_revoked_actor_cannot_read_report_card():
    class Reports:
        async def get(self, **kwargs: Any):
            raise ValueError("revoked")

    projector = ReportBlockProjector(reports=Reports(), contexts=None)
    result = await projector.project(
        None,
        {
            "kind": "report",
            "report_id": str(uuid4()),
            "href": "https://attacker.test",
            "metrics": [{"value": "secret"}],
        },
    )
    assert result["kind"] == "safe_error"
    assert "secret" not in str(result)
    assert "attacker" not in str(result)


@pytest.mark.asyncio
async def test_status_card_requires_recorded_current_authority():
    class Contexts:
        async def project(self, actor: Any, value: Any):
            raise ValueError("foreign context")

    result = await ReportBlockProjector(reports=None, contexts=Contexts()).project(
        None, [{"kind": "project_status", "context_run_id": str(uuid4()), "analysis": ["secret"]}]
    )
    assert result[0]["kind"] == "safe_error"
    assert "secret" not in str(result)
