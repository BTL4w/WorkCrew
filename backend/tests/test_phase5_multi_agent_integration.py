"""Real PostgreSQL handoff provenance and failure/manual publication boundary."""

from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import BaseModel

from app.core.config import Settings
from app.modules.planning_runs.adapters.ai_runtime import build_model_gateway
from app.modules.reporting.domain.commands import CreateReportCommand, PublishReportCommand
from tests.test_report_api_integration import ReportHarness, pytestmark, report_harness
from tests.test_report_review_integration import worker
from work_management_ai.model_gateway.contracts import (
    StructuredModelRequest,
    StructuredModelResponse,
)
from work_management_ai.model_gateway.errors import ModelInvalidOutputError

__all__ = ["pytestmark", "report_harness"]


@pytest.mark.asyncio
async def test_reporting_handoffs_are_hub_only_and_current_actor_scoped(
    report_harness: ReportHarness,
):
    h = report_harness
    for locale in ("vi", "en"):
        r = await h.service.create(
            actor=h.actor,
            command=CreateReportCommand(project_id=h.project_id, locale=locale),
            idempotency_key=str(uuid4()),
        )
        assert await worker(h)
        ready = await h.service.get(actor=h.actor, report_id=r.report.id)
        assert ready.selected_version.origin == "AI_PROPOSED" and ready.publications == ()
    rows = (
        await h.sql(
            "SELECT h.organization_id,r.actor_membership_id,p.agent_id,h.target_agent_id "
            "FROM agent_handoffs h JOIN orchestration_runs r ON r.id=h.orchestration_run_id "
            "AND r.organization_id=h.organization_id "
            "JOIN agent_runs p ON p.id=h.parent_agent_run_id "
            "AND p.organization_id=h.organization_id WHERE h.organization_id=:org",
            {"org": h.actor.organization_id},
        )
    ).all()
    assert len(rows) == 2
    assert all(
        tuple(row) == (h.actor.organization_id, h.actor.membership_id, "orchestrator", "reporting")
        for row in rows
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["disabled", "invalid", "semantic"])
async def test_report_fallback_does_not_publish_invalid_narrative(
    report_harness: ReportHarness, failure: str
):
    h = report_harness
    r = await h.service.create(
        actor=h.actor,
        command=CreateReportCommand(project_id=h.project_id),
        idempotency_key=str(uuid4()),
    )

    class FailingGateway:
        async def generate_structured[T: BaseModel](
            self, request: StructuredModelRequest[T]
        ) -> StructuredModelResponse[T]:
            if failure == "invalid":
                raise ModelInvalidOutputError("synthetic invalid output")
            response = await build_model_gateway(
                Settings(environment="test", ai_provider="mock")
            ).generate_structured(request)
            if request.invocation_key.endswith(".grounding"):
                return replace(
                    response,
                    parsed=request.output_schema.model_validate(
                        {"passed": False, "claim_verdicts": [], "safe_codes": ["REJECTED"]}
                    ),
                )
            return response

    gateway = (
        build_model_gateway(Settings(environment="test", ai_provider="disabled"))
        if failure == "disabled"
        else FailingGateway()
    )
    assert await worker(h, gateway)
    fallback = await h.service.get(actor=h.actor, report_id=r.report.id)
    assert fallback.generation_state == "AI_UNAVAILABLE"
    assert fallback.selected_version.origin == "METRICS_ONLY"
    assert fallback.publications == ()
    published = await h.service.publish_metrics(
        actor=h.actor,
        report_id=r.report.id,
        command=PublishReportCommand(
            mode="METRICS_ONLY",
            report_version_id=r.selected_version.id,
            snapshot_hash=r.snapshot.snapshot_hash,
        ),
        expected_version=fallback.report.version,
        idempotency_key=str(uuid4()),
    )
    assert published.publications[0].report_version_id == r.selected_version.id
    assert published.snapshot == r.snapshot
    assert (
        await h.sql(
            "SELECT count(*) FROM audit_events WHERE organization_id=:org",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() > 0
