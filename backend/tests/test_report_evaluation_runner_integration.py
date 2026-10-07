from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.feedback.adapters.evaluation_runner import (
    EvaluationRunner,
    FrozenDatasetReader,
    ReportingEvaluationProvider,
)
from app.modules.feedback.adapters.transaction import CurationTransactions
from app.modules.feedback.domain.evaluation import (
    DatasetCommand,
    EvaluationDatasetVersion,
    ReportEvaluationResult,
)
from app.modules.reporting.domain.reports import ReportError
from tests.test_evaluation_curation_integration import admin_service, command
from tests.test_feedback_outcome_integration import reviewed
from tests.test_report_api_integration import ReportHarness, pytestmark, report_harness

__all__ = ["pytestmark", "report_harness"]


async def frozen_dataset(h: ReportHarness):
    _, feedback = await reviewed(h)
    service = await admin_service(h)
    candidate = await service.prepare(
        actor=h.actor, feedback_id=feedback, idempotency_key=str(uuid4())
    )
    await service.curate(
        actor=h.actor,
        command=command(candidate, locale="vi"),
        expected_version=1,
        idempotency_key=str(uuid4()),
    )
    await service.curate(
        actor=h.actor,
        command=command(candidate, locale="en"),
        expected_version=2,
        idempotency_key=str(uuid4()),
    )
    return await service.freeze_dataset(
        actor=h.actor,
        command=DatasetCommand(name="report-eval", split="GOLDEN"),
        idempotency_key=str(uuid4()),
    )


@pytest.mark.asyncio
async def test_frozen_dataset_bridge_retains_exact_provenance_and_does_not_certify_missing_coverage(
    report_harness: ReportHarness,
):
    h = report_harness
    dataset = await frozen_dataset(h)
    runner = EvaluationRunner(
        FrozenDatasetReader(
            CurationTransactions(async_sessionmaker(h.engine, expire_on_commit=False), "UTC")
        )
    )
    result = await runner.run(actor=h.actor, dataset_version_id=dataset.id)
    assert result.dataset_hash == dataset.dataset_hash
    assert result.passed == 2 and result.failed == 0
    missing = result.gate["missing_coverage"]
    assert isinstance(missing, list)
    assert not result.gate_passed and "sources" in missing
    assert result.hosted_quality == "NOT_RUN"
    hashes: set[str] = set()
    for item in result.cases:
        provenance = item["provenance"]
        assert isinstance(provenance, dict)
        digest = provenance["case_hash"]
        assert isinstance(digest, str)
        hashes.add(digest)
    assert hashes == {case.case_hash for case in dataset.cases}
    for actor in (h.employee, h.foreign):
        with pytest.raises(ReportError):
            await runner.run(actor=actor, dataset_version_id=dataset.id)


@pytest.mark.asyncio
async def test_permissions_are_rechecked_after_external_evaluation(report_harness: ReportHarness):
    h = report_harness
    dataset = await frozen_dataset(h)

    class RevokingProvider:
        async def evaluate(self, dataset: EvaluationDatasetVersion) -> ReportEvaluationResult:
            result = await ReportingEvaluationProvider().evaluate(dataset)
            await h.sql(
                "UPDATE memberships SET role='EMPLOYEE' WHERE id=:id", {"id": h.actor.membership_id}
            )
            return result

    runner = EvaluationRunner(
        FrozenDatasetReader(
            CurationTransactions(async_sessionmaker(h.engine, expire_on_commit=False), "UTC")
        ),
        RevokingProvider(),
    )
    with pytest.raises(ReportError, match="FORBIDDEN"):
        await runner.run(actor=h.actor, dataset_version_id=dataset.id)


@pytest.mark.asyncio
async def test_trusted_cli_emits_only_safe_metadata_and_mock_default(
    report_harness: ReportHarness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    import argparse

    from app.core.config import Settings
    from app.scripts import evaluate_reports as cli

    h = report_harness
    dataset = await frozen_dataset(h)
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(environment="test"))
    args = argparse.Namespace(
        organization_id=h.actor.organization_id,
        membership_id=h.actor.membership_id,
        dataset_version=dataset.id,
        provider="mock",
        budget_tokens=None,
        judge=False,
    )
    assert not await cli.evaluate(args)
    result = ReportEvaluationResult.model_validate_json(capsys.readouterr().out)
    assert result.dataset_hash == dataset.dataset_hash and result.provider == "mock"
    for secret in ("UNTRUSTED_CONTEXT", '"messages":', '"narrative":', "password", "reasoning"):
        assert secret not in result.model_dump_json()
    args.provider = "hosted"
    with pytest.raises(ReportError, match="HOSTED_DISABLED"):
        await cli.evaluate(args)
