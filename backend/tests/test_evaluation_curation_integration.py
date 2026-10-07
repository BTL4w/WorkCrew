from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.feedback.adapters.transaction import CurationTransactions
from app.modules.feedback.application.curation_service import CurationService
from app.modules.feedback.domain.evaluation import (
    CurateCaseCommand,
    DatasetCaseSelection,
    DatasetCommand,
    EvaluationCandidate,
)
from app.modules.reporting.domain.reports import ReportError
from tests.test_evaluation_curation import assertion, payload
from tests.test_feedback_outcome_integration import reviewed
from tests.test_report_api_integration import ReportHarness, pytestmark, report_harness

__all__ = ["pytestmark", "report_harness"]


async def admin_service(h: ReportHarness):
    await h.sql("UPDATE memberships SET role='ADMIN' WHERE id=:id", {"id": h.actor.membership_id})
    return CurationService(
        CurationTransactions(async_sessionmaker(h.engine, expire_on_commit=False), "UTC")
    )


def command(
    candidate: EvaluationCandidate,
    value: str = "2",
    split: Literal["GOLDEN", "HELD_OUT"] = "GOLDEN",
    locale: Literal["vi", "en"] = "en",
) -> CurateCaseCommand:
    return CurateCaseCommand(
        candidate_id=candidate.id,
        payload=payload(locale=locale, value=value),
        expected_assertions=(assertion(value),),
        origin="SYNTHETIC",
        split=split,
        human_review_decision="APPROVE",
        permission_reviewed=True,
    )


@pytest.mark.asyncio
async def test_candidate_is_not_automatically_golden(report_harness: ReportHarness):
    h = report_harness
    _, feedback_id = await reviewed(h)
    service = await admin_service(h)
    key = str(uuid4())
    candidate = await service.prepare(actor=h.actor, feedback_id=feedback_id, idempotency_key=key)
    assert candidate.status == "PENDING_REVIEW"
    assert candidate.feedback_id == feedback_id
    assert candidate.payload_classification == "RAW_CANDIDATE"
    assert (candidate.expires_at - candidate.created_at).days == 30
    assert (
        await service.prepare(actor=h.actor, feedback_id=feedback_id, idempotency_key=key)
    ).id == candidate.id
    assert (
        await h.sql(
            "SELECT count(*) FROM evaluation_cases WHERE organization_id=:org",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 0
    with pytest.raises(ReportError):
        await service.freeze_dataset(
            actor=h.actor,
            command=DatasetCommand(name="default", split="GOLDEN"),
            idempotency_key=str(uuid4()),
        )


@pytest.mark.asyncio
async def test_frozen_dataset_preserves_exact_case_versions(report_harness: ReportHarness):
    h = report_harness
    _, feedback_id = await reviewed(h)
    service = await admin_service(h)
    candidate = await service.prepare(
        actor=h.actor, feedback_id=feedback_id, idempotency_key=str(uuid4())
    )
    first = await service.curate(
        actor=h.actor, command=command(candidate), expected_version=1, idempotency_key=str(uuid4())
    )
    frozen_command = DatasetCommand(
        name="reports",
        split="GOLDEN",
        cases=(DatasetCaseSelection(case_id=first.id, version=first.version),),
    )
    key = str(uuid4())
    frozen = await service.freeze_dataset(
        actor=h.actor, command=frozen_command, idempotency_key=key
    )
    changed = await service.curate(
        actor=h.actor,
        command=command(candidate, value="3"),
        expected_version=2,
        idempotency_key=str(uuid4()),
    )
    assert changed.id == first.id and changed.version == 2
    assert changed.case_hash != first.case_hash
    replay = await service.freeze_dataset(
        actor=h.actor, command=frozen_command, idempotency_key=key
    )
    assert replay == frozen
    assert frozen.cases[0].case_hash == first.case_hash
    assert frozen.cases[0].version == 1
    with pytest.raises(ReportError, match="SPLIT_CONFLICT"):
        await service.curate(
            actor=h.actor,
            command=command(candidate, value="3", split="HELD_OUT"),
            expected_version=3,
            idempotency_key=str(uuid4()),
        )


@pytest.mark.asyncio
async def test_permission_review_stale_replay_and_audit(report_harness: ReportHarness):
    h = report_harness
    _, feedback_id = await reviewed(h)
    service = CurationService(
        CurationTransactions(async_sessionmaker(h.engine, expire_on_commit=False), "UTC")
    )
    with pytest.raises(ReportError, match="FORBIDDEN"):
        await service.prepare(actor=h.actor, feedback_id=feedback_id, idempotency_key=str(uuid4()))
    service = await admin_service(h)
    prepare_key = str(uuid4())
    candidate = await service.prepare(
        actor=h.actor, feedback_id=feedback_id, idempotency_key=prepare_key
    )
    for actor in (h.employee, h.foreign):
        with pytest.raises(ReportError):
            await service.prepare(
                actor=actor, feedback_id=feedback_id, idempotency_key=str(uuid4())
            )
    for updates in ({"permission_reviewed": False}, {"human_review_decision": "REJECT"}):
        with pytest.raises(ReportError, match="REVIEW_REQUIRED"):
            await service.curate(
                actor=h.actor,
                command=command(candidate).model_copy(update=updates),
                expected_version=1,
                idempotency_key=str(uuid4()),
            )
    assert (
        await h.sql(
            "SELECT count(*) FROM evaluation_cases WHERE organization_id=:org",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 0
    key = str(uuid4())
    result = await service.curate(
        actor=h.actor, command=command(candidate), expected_version=1, idempotency_key=key
    )
    assert (
        await service.curate(
            actor=h.actor, command=command(candidate), expected_version=1, idempotency_key=key
        )
        == result
    )
    with pytest.raises(ReportError, match="IDEMPOTENCY_KEY_REUSED"):
        await service.curate(
            actor=h.actor,
            command=command(candidate, value="3"),
            expected_version=1,
            idempotency_key=key,
        )
    with pytest.raises(ReportError, match="STALE_VERSION"):
        await service.curate(
            actor=h.actor,
            command=command(candidate, value="3"),
            expected_version=1,
            idempotency_key=str(uuid4()),
        )
    await h.sql(
        "UPDATE memberships SET role='EMPLOYEE' WHERE id=:id", {"id": h.actor.membership_id}
    )
    with pytest.raises(ReportError, match="FORBIDDEN"):
        await service.prepare(actor=h.actor, feedback_id=feedback_id, idempotency_key=prepare_key)
    with pytest.raises(ReportError, match="FORBIDDEN"):
        await service.curate(
            actor=h.actor, command=command(candidate), expected_version=1, idempotency_key=key
        )
    events = (
        (
            await h.sql(
                "SELECT outcome FROM audit_events WHERE organization_id=:org AND "
                "action LIKE 'evaluation.%'",
                {"org": h.actor.organization_id},
            )
        )
        .scalars()
        .all()
    )
    assert events.count("SUCCEEDED") == 2 and events.count("REJECTED") >= 7


@pytest.mark.asyncio
async def test_deduplication_and_split_separation(report_harness: ReportHarness):
    h = report_harness
    _, feedback_id = await reviewed(h)
    service = await admin_service(h)
    first = await service.prepare(
        actor=h.actor, feedback_id=feedback_id, idempotency_key=str(uuid4())
    )
    original = await service.curate(
        actor=h.actor,
        command=command(first, value="2.00"),
        expected_version=1,
        idempotency_key=str(uuid4()),
    )
    _, second_feedback = await reviewed(h)
    second = await service.prepare(
        actor=h.actor, feedback_id=second_feedback, idempotency_key=str(uuid4())
    )
    duplicate = await service.curate(
        actor=h.actor,
        command=command(second, value="2"),
        expected_version=1,
        idempotency_key=str(uuid4()),
    )
    assert duplicate == original
    with pytest.raises(ReportError, match="SPLIT_CONFLICT"):
        await service.curate(
            actor=h.actor,
            command=command(second, value="2", split="HELD_OUT"),
            expected_version=2,
            idempotency_key=str(uuid4()),
        )
    vi = await service.curate(
        actor=h.actor,
        command=command(second, value="4", split="HELD_OUT", locale="vi"),
        expected_version=2,
        idempotency_key=str(uuid4()),
    )
    frozen = await service.freeze_dataset(
        actor=h.actor,
        command=DatasetCommand(name="held", split="HELD_OUT"),
        idempotency_key=str(uuid4()),
    )
    assert frozen.cases == (vi,) and frozen.verified_hash()
    golden = await service.freeze_dataset(
        actor=h.actor,
        command=DatasetCommand(name="golden", split="GOLDEN"),
        idempotency_key=str(uuid4()),
    )
    assert golden.cases == (original,) and golden.verified_hash()
    assert (
        await service.freeze_dataset(
            actor=h.actor,
            command=DatasetCommand(name="golden", split="GOLDEN"),
            idempotency_key=str(uuid4()),
        )
        == golden
    )


@pytest.mark.asyncio
async def test_redacted_values_require_source_match_and_explicit_dataset_opt_in(
    report_harness: ReportHarness,
):
    h = report_harness
    _, feedback_id = await reviewed(h)
    service = await admin_service(h)
    candidate = await service.prepare(
        actor=h.actor, feedback_id=feedback_id, idempotency_key=str(uuid4())
    )
    assert candidate.payload is not None
    metric = candidate.payload.metrics["tasks.status.total_count"]
    valid = command(candidate, value=str(metric.value)).model_copy(update={"origin": "REDACTED"})
    invalid = valid.model_copy(
        update={"payload": payload(value="999"), "expected_assertions": (assertion("999"),)}
    )
    with pytest.raises(ReportError, match="SOURCE_MISMATCH"):
        await service.curate(
            actor=h.actor, command=invalid, expected_version=1, idempotency_key=str(uuid4())
        )
    case = await service.curate(
        actor=h.actor, command=valid, expected_version=1, idempotency_key=str(uuid4())
    )
    with pytest.raises(ReportError, match="EMPTY"):
        await service.freeze_dataset(
            actor=h.actor,
            command=DatasetCommand(name="default", split="GOLDEN"),
            idempotency_key=str(uuid4()),
        )
    frozen = await service.freeze_dataset(
        actor=h.actor,
        command=DatasetCommand(name="redacted", split="GOLDEN", include_redacted=True),
        idempotency_key=str(uuid4()),
    )
    assert frozen.cases == (case,)


@pytest.mark.asyncio
async def test_reasoned_reject_uses_exact_generation_without_copying_rejected_text(
    report_harness: ReportHarness,
):
    from app.modules.reporting.domain.commands import RejectReportCommand
    from tests.test_report_review_integration import ready, reviews

    h = report_harness
    r = await ready(h)
    review = await reviews(h).reject(
        actor=h.actor,
        report_id=r.report.id,
        command=RejectReportCommand(
            report_version_id=r.selected_version.id,
            snapshot_hash=r.snapshot.snapshot_hash,
            reason="Wrong claim Alice alice@example.test sk-private",
        ),
        expected_version=r.report.version,
        idempotency_key=str(uuid4()),
    )
    service = await admin_service(h)
    candidate = await service.prepare(
        actor=h.actor, feedback_id=review.terminal_outcome_id, idempotency_key=str(uuid4())
    )
    assert candidate.generation_id == r.selected_version.generation_id
    assert candidate.original_version_id == r.selected_version.id
    for private in ("Alice", "alice@example.test", "sk-private", "Wrong claim"):
        assert private not in candidate.model_dump_json()
    case = await service.curate(
        actor=h.actor, command=command(candidate), expected_version=1, idempotency_key=str(uuid4())
    )
    assert case.provenance["generation_id"] == str(r.selected_version.generation_id)
    versions = case.provenance["generation_versions"]
    assert isinstance(versions, dict)
    assert "numeric_verdict" not in versions
    # Advisory dislike carries no terminal human quality decision and cannot enter a dataset.
    from app.modules.feedback.adapters.transaction import FeedbackTransactions
    from app.modules.feedback.application.feedback_service import FeedbackService
    from app.modules.feedback.domain.feedback import FeedbackCommand

    feedback = await FeedbackService(
        FeedbackTransactions(async_sessionmaker(h.engine, expire_on_commit=False), "UTC")
    ).record(
        actor=h.actor,
        command=FeedbackCommand(
            report_id=r.report.id,
            report_version_id=r.selected_version.id,
            decision="REJECT",
            reason=" ",
        ),
        idempotency_key=str(uuid4()),
    )
    with pytest.raises(ReportError, match="REASON_REQUIRED"):
        await service.prepare(
            actor=h.actor, feedback_id=feedback.feedback.id, idempotency_key=str(uuid4())
        )


@pytest.mark.asyncio
async def test_rls_composite_fks_immutable_history_and_expired_context(
    report_harness: ReportHarness,
):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    h = report_harness
    _, feedback_id = await reviewed(h)
    service = await admin_service(h)
    candidate = await service.prepare(
        actor=h.actor, feedback_id=feedback_id, idempotency_key=str(uuid4())
    )
    first = await service.curate(
        actor=h.actor, command=command(candidate), expected_version=1, idempotency_key=str(uuid4())
    )
    key = str(uuid4())
    request = DatasetCommand(name="protected", split="GOLDEN")
    frozen = await service.freeze_dataset(actor=h.actor, command=request, idempotency_key=key)
    await h.sql("UPDATE memberships SET role='ADMIN' WHERE id=:id", {"id": h.foreign.membership_id})
    for actor in (h.employee, h.foreign):
        with pytest.raises(ReportError):
            await service.curate(
                actor=actor,
                command=command(candidate),
                expected_version=2,
                idempotency_key=str(uuid4()),
            )
        with pytest.raises(ReportError):
            await service.freeze_dataset(
                actor=actor,
                command=DatasetCommand(
                    name="foreign",
                    split="GOLDEN",
                    cases=(DatasetCaseSelection(case_id=first.id, version=1),),
                ),
                idempotency_key=str(uuid4()),
            )
        async with h.engine.connect() as connection, connection.begin():
            await connection.execute(text("SET LOCAL ROLE app_runtime"))
            await connection.execute(
                text(
                    "SELECT "
                    "set_config('app.organization_id',:org,true),set_config('app.membership"
                    "_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            for table in (
                "evaluation_candidates",
                "evaluation_cases",
                "evaluation_case_revisions",
                "evaluation_dataset_versions",
                "evaluation_dataset_cases",
            ):
                assert (
                    await connection.execute(text(f"SELECT count(*) FROM {table}"))
                ).scalar_one() == 0
    with pytest.raises(DBAPIError) as foreign:
        await h.sql(
            "INSERT INTO evaluation_cases "
            "(id,organization_id,candidate_id,locale,split,current_version) VALUES "
            "(:id,:org,:candidate,'vi','GOLDEN',1)",
            {"id": uuid4(), "org": h.foreign.organization_id, "candidate": candidate.id},
        )
    assert getattr(foreign.value.orig, "sqlstate", None) == "23503"
    with pytest.raises(DBAPIError) as curator:
        await h.sql(
            "INSERT INTO evaluation_case_revisions SELECT "
            ":id,organization_id,case_id,version+1,:hash,origin,split,policy_version,:membe"
            "r,body,created_at FROM evaluation_case_revisions WHERE id=:revision",
            {
                "id": uuid4(),
                "hash": "f" * 64,
                "member": h.foreign.membership_id,
                "revision": first.revision_id,
            },
        )
    assert getattr(curator.value.orig, "sqlstate", None) == "23503"
    with pytest.raises(DBAPIError) as membership:
        await h.sql(
            "INSERT INTO "
            "evaluation_dataset_cases(id,organization_id,dataset_id,case_id,case_version,ca"
            "se_hash) VALUES (:id,:org,:dataset,:case,2,:hash)",
            {
                "id": uuid4(),
                "org": h.actor.organization_id,
                "dataset": frozen.id,
                "case": first.id,
                "hash": "e" * 64,
            },
        )
    assert getattr(membership.value.orig, "sqlstate", None) == "23503"
    for statement, identity in (
        ("UPDATE evaluation_case_revisions SET body='{}' WHERE id=:id", first.revision_id),
        ("DELETE FROM evaluation_case_revisions WHERE id=:id", first.revision_id),
        ("UPDATE evaluation_dataset_versions SET manifest='{}' WHERE id=:id", frozen.id),
        ("DELETE FROM evaluation_dataset_cases WHERE dataset_id=:id", frozen.id),
        ("UPDATE evaluation_cases SET split='HELD_OUT' WHERE id=:id", first.id),
        ("UPDATE evaluation_candidates SET context='{}' WHERE id=:id", candidate.id),
    ):
        with pytest.raises(DBAPIError):
            await h.sql(statement, {"id": identity})
    # Raw candidate context may expire/purge without deleting reviewed cases or business audit.
    await h.sql("UPDATE evaluation_candidates SET context=NULL WHERE id=:id", {"id": candidate.id})
    with pytest.raises(ReportError, match="CONTEXT_EXPIRED"):
        await service.curate(
            actor=h.actor,
            command=command(candidate),
            expected_version=2,
            idempotency_key=str(uuid4()),
        )
    assert (
        await service.freeze_dataset(actor=h.actor, command=request, idempotency_key=key) == frozen
    )
    with pytest.raises(ReportError, match="CONTEXT_EXPIRED"):
        await service.prepare(actor=h.actor, feedback_id=feedback_id, idempotency_key=str(uuid4()))


@pytest.mark.asyncio
async def test_cli_bilingual_prepare_review_freeze(
    report_harness: ReportHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    import argparse

    from app.core.config import Settings
    from app.modules.feedback.domain.evaluation import (
        EvaluationDatasetVersion,
        EvaluationReviewDiff,
    )
    from app.scripts import curate_report_case as cli

    h = report_harness
    _, feedback_id = await reviewed(h)
    await admin_service(h)
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(environment="test"))

    def arguments(action: str, **values: Any) -> argparse.Namespace:
        return argparse.Namespace(
            action=action,
            organization_id=h.actor.organization_id,
            membership_id=h.actor.membership_id,
            idempotency_key=str(uuid4()),
            **values,
        )

    await cli.run(arguments("prepare", feedback_id=feedback_id))
    candidate = EvaluationCandidate.model_validate_json(capsys.readouterr().out)
    for index, locale in enumerate(("vi", "en")):
        source = tmp_path / f"{locale}.json"
        source.write_text(command(candidate, locale=locale).model_dump_json())
        if index == 0:
            await cli.run(
                arguments(
                    "curate",
                    command_file=source,
                    expected_version=1,
                    approve_reviewed=False,
                    preview=True,
                )
            )
            diff = EvaluationReviewDiff.model_validate_json(capsys.readouterr().out)
            assert diff.candidate_id == candidate.id and diff.candidate_version == 1
            with pytest.raises(ReportError, match="REVIEW_REQUIRED"):
                await cli.run(
                    arguments(
                        "curate", command_file=source, expected_version=1, approve_reviewed=False
                    )
                )
        await cli.run(
            arguments(
                "curate", command_file=source, expected_version=index + 1, approve_reviewed=True
            )
        )
        capsys.readouterr()
    source = tmp_path / "dataset.json"
    source.write_text(DatasetCommand(name="synthetic-bilingual", split="GOLDEN").model_dump_json())
    await cli.run(arguments("freeze", command_file=source))
    frozen = EvaluationDatasetVersion.model_validate_json(capsys.readouterr().out)
    assert {case.payload.locale for case in frozen.cases} == {"vi", "en"}
    assert len(frozen.cases) == 2 and frozen.verified_hash()
    args = arguments("prepare", feedback_id=feedback_id)
    args.membership_id = h.employee.membership_id
    with pytest.raises(ReportError, match="FORBIDDEN"):
        await cli.run(args)


@pytest.mark.asyncio
async def test_prepare_replay_is_exact_after_candidate_review(report_harness: ReportHarness):
    h = report_harness
    _, feedback_id = await reviewed(h)
    service = await admin_service(h)
    key = str(uuid4())
    candidate = await service.prepare(actor=h.actor, feedback_id=feedback_id, idempotency_key=key)
    await service.curate(
        actor=h.actor, command=command(candidate), expected_version=1, idempotency_key=str(uuid4())
    )
    assert (
        await service.prepare(actor=h.actor, feedback_id=feedback_id, idempotency_key=key)
        == candidate
    )
    current = await service.prepare(
        actor=h.actor, feedback_id=feedback_id, idempotency_key=str(uuid4())
    )
    assert current.version == 2 and current.status == "CURATED"


@pytest.mark.asyncio
async def test_curate_replay_survives_raw_candidate_purge(report_harness: ReportHarness):
    h = report_harness
    _, feedback_id = await reviewed(h)
    service = await admin_service(h)
    candidate = await service.prepare(
        actor=h.actor, feedback_id=feedback_id, idempotency_key=str(uuid4())
    )
    key = str(uuid4())
    first = await service.curate(
        actor=h.actor, command=command(candidate), expected_version=1, idempotency_key=key
    )
    await h.sql("UPDATE evaluation_candidates SET context=NULL WHERE id=:id", {"id": candidate.id})
    assert (
        await service.curate(
            actor=h.actor, command=command(candidate), expected_version=1, idempotency_key=key
        )
        == first
    )
    with pytest.raises(ReportError, match="CONTEXT_EXPIRED"):
        await service.curate(
            actor=h.actor,
            command=command(candidate),
            expected_version=2,
            idempotency_key=str(uuid4()),
        )


@pytest.mark.asyncio
async def test_human_review_preview_shows_numeric_diff_without_writes(
    report_harness: ReportHarness,
):
    h = report_harness
    _, feedback_id = await reviewed(h)
    service = await admin_service(h)
    candidate = await service.prepare(
        actor=h.actor, feedback_id=feedback_id, idempotency_key=str(uuid4())
    )
    proposed = command(candidate, value="3").model_copy(
        update={"human_review_decision": "REJECT", "permission_reviewed": False}
    )
    preview = await service.preview(actor=h.actor, command=proposed, expected_version=1)
    assert (
        preview.before.metrics["tasks.status.total_count"].value
        != preview.after.metrics["tasks.status.total_count"].value
    )
    assert preview.after.metrics["tasks.status.total_count"].value == 3
    assert (
        await h.sql(
            "SELECT count(*) FROM evaluation_cases WHERE organization_id=:org",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 0
    for actor in (h.employee, h.foreign):
        with pytest.raises(ReportError):
            await service.preview(actor=actor, command=proposed, expected_version=1)


@pytest.mark.asyncio
async def test_source_outcomes_are_reauthorized_even_on_replay(report_harness: ReportHarness):
    from app.modules.feedback.adapters.transaction import FeedbackTransactions
    from app.modules.feedback.application.outcome_service import OutcomeService
    from app.modules.feedback.domain.outcomes import OutcomeSourceCommand

    h = report_harness
    _, feedback_id = await reviewed(h)
    outcome = await OutcomeService(
        FeedbackTransactions(async_sessionmaker(h.engine, expire_on_commit=False), "UTC")
    ).record(
        actor=h.actor,
        feedback_id=feedback_id,
        source=OutcomeSourceCommand(
            source_type="TASK_ACTUALS", source_id=h.task_id, source_version=0
        ),
        idempotency_key=str(uuid4()),
    )
    service = await admin_service(h)
    candidate = await service.prepare(
        actor=h.actor, feedback_id=feedback_id, idempotency_key=str(uuid4())
    )
    assert candidate.outcome_ids == (outcome.id,)
    key = str(uuid4())
    await service.curate(
        actor=h.actor, command=command(candidate), expected_version=1, idempotency_key=key
    )
    dataset = DatasetCommand(name="outcomes", split="GOLDEN")
    frozen_key = str(uuid4())
    await service.freeze_dataset(actor=h.actor, command=dataset, idempotency_key=frozen_key)
    project = uuid4()
    await h.sql(
        "INSERT INTO "
        "projects(id,organization_id,name,created_by_membership_id,updated_by_membership_id"
        ") VALUES (:id,:org,'Other',:member,:member)",
        {"id": project, "org": h.actor.organization_id, "member": h.actor.membership_id},
    )
    await h.sql(
        "UPDATE tasks SET project_id=:project WHERE id=:id", {"project": project, "id": h.task_id}
    )
    await h.sql("UPDATE evaluation_candidates SET context=NULL WHERE id=:id", {"id": candidate.id})
    with pytest.raises(ReportError, match="RESOURCE_NOT_FOUND"):
        await service.curate(
            actor=h.actor, command=command(candidate), expected_version=1, idempotency_key=key
        )
    with pytest.raises(ReportError, match="RESOURCE_NOT_FOUND"):
        await service.freeze_dataset(actor=h.actor, command=dataset, idempotency_key=frozen_key)


@pytest.mark.asyncio
async def test_context_expiration_guard_preserves_reviewed_replay(
    report_harness: ReportHarness, monkeypatch: pytest.MonkeyPatch
):
    from datetime import timedelta

    from app.modules.feedback.adapters.curation_repository import SQLCurationRepository

    h = report_harness
    _, feedback_id = await reviewed(h)
    service = await admin_service(h)
    candidate = await service.prepare(
        actor=h.actor, feedback_id=feedback_id, idempotency_key=str(uuid4())
    )
    key = str(uuid4())
    first = await service.curate(
        actor=h.actor, command=command(candidate), expected_version=1, idempotency_key=key
    )

    async def clock(self: SQLCurationRepository):
        return candidate.created_at + timedelta(days=31)

    monkeypatch.setattr(SQLCurationRepository, "captured_at", clock)
    with pytest.raises(ReportError, match="CONTEXT_EXPIRED"):
        await service.prepare(actor=h.actor, feedback_id=feedback_id, idempotency_key=str(uuid4()))
    assert (
        await service.curate(
            actor=h.actor, command=command(candidate), expected_version=1, idempotency_key=key
        )
        == first
    )


@pytest.mark.asyncio
async def test_reasoned_edit_links_original_and_reviewed_versions(report_harness: ReportHarness):
    from app.modules.reporting.domain.commands import EditReportCommand
    from tests.test_report_review_integration import publish_command, ready, reviews, worker
    from work_management_ai.agents.reporting.contracts import NarrativeTextBlock, SourceIdentity

    h = report_harness
    original = await ready(h)
    assert original.selected_version.narrative is not None
    extra = NarrativeTextBlock(
        id="advice",
        section="recommendations",
        kind="RECOMMENDATION",
        text="Review the captured work before deciding next steps.",
        source_refs=(
            SourceIdentity.model_validate(
                original.snapshot.sources[0].model_dump(
                    include={"resource_type", "resource_id", "version", "fingerprint"}
                )
            ),
        ),
        assumptions=("Team review is available",),
    )
    edited = await reviews(h).edit(
        actor=h.actor,
        report_id=original.report.id,
        command=EditReportCommand(
            parent_version_id=original.selected_version.id,
            snapshot_hash=original.snapshot.snapshot_hash,
            narrative=original.selected_version.narrative.model_copy(
                update={"blocks": (*original.selected_version.narrative.blocks, extra)}
            ),
        ),
        expected_version=original.report.version,
        idempotency_key=str(uuid4()),
    )
    assert await worker(h)
    checked = await h.service.get(actor=h.actor, report_id=original.report.id)
    terminal = await reviews(h).publish(
        actor=h.actor,
        report_id=checked.report.id,
        command=publish_command(checked),
        expected_version=checked.report.version,
        idempotency_key=str(uuid4()),
    )
    service = await admin_service(h)
    candidate = await service.prepare(
        actor=h.actor, feedback_id=terminal.terminal_outcome_id, idempotency_key=str(uuid4())
    )
    assert candidate.generation_id == original.selected_version.generation_id
    assert candidate.original_version_id == original.selected_version.id
    assert candidate.report_version_id == edited.selected_version.id
    assert candidate.snapshot_hash == original.snapshot.snapshot_hash
    case = await service.curate(
        actor=h.actor, command=command(candidate), expected_version=1, idempotency_key=str(uuid4())
    )
    assert case.provenance["original_version_id"] == str(original.selected_version.id)
    assert case.provenance["report_version_id"] == str(edited.selected_version.id)
