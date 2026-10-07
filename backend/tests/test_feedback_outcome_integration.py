from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.feedback.adapters.transaction import FeedbackTransactions
from app.modules.feedback.application.feedback_service import FeedbackService
from app.modules.feedback.application.outcome_service import OutcomeService
from app.modules.feedback.domain.outcomes import OutcomeSourceCommand
from app.modules.reporting.domain.reports import ReportError
from tests.test_report_api_integration import ReportHarness, pytestmark, report_harness
from tests.test_report_review_integration import publish_command, ready, reviews

__all__ = ["pytestmark", "report_harness"]


def services(h: ReportHarness):
    transactions = FeedbackTransactions(async_sessionmaker(h.engine, expire_on_commit=False), "UTC")
    return OutcomeService(transactions), FeedbackService(transactions)


async def reviewed(h: ReportHarness):
    r = await ready(h)
    result = await reviews(h).publish(
        actor=h.actor,
        report_id=r.report.id,
        command=publish_command(r),
        expected_version=r.report.version,
        idempotency_key=str(uuid4()),
    )
    return r, result.terminal_outcome_id


@pytest.mark.asyncio
async def test_outcome_requires_verified_business_fact(report_harness: ReportHarness):
    h = report_harness
    r, feedback_id = await reviewed(h)
    outcomes, feedback = services(h)
    missing = OutcomeSourceCommand(
        source_type="TASK_ACTUALS", source_id=h.task_id, source_version=0
    )
    result = await outcomes.record(
        actor=h.actor, feedback_id=feedback_id, source=missing, idempotency_key=str(uuid4())
    )
    assert result.state == "UNKNOWN"
    transition = uuid4()
    await h.sql(
        "INSERT INTO task_status_transitions(id,organization_id,task_id,from_status,"
        "to_status,actor_membership_id,task_version_after) VALUES (:id,:org,:task,'I"
        "N_PROGRESS','DONE',:member,2)",
        {
            "id": transition,
            "org": h.actor.organization_id,
            "task": h.task_id,
            "member": h.actor.membership_id,
        },
    )
    done = await outcomes.record(
        actor=h.actor,
        feedback_id=feedback_id,
        source=OutcomeSourceCommand(
            source_type="TASK_TRANSITION", source_id=transition, source_version=2
        ),
        idempotency_key=str(uuid4()),
    )
    assert done.state == "AVAILABLE"
    assert done.facts["to_status"] == "DONE"
    assert (await feedback.rates(actor=h.actor, project_id=h.project_id)).accept_percent == 100
    projection = await h.service.get(actor=h.actor, report_id=r.report.id)
    assert len(projection.feedback) == 1
    assert len(projection.feedback_outcomes) == 2


@pytest.mark.asyncio
async def test_outcomes_append_and_remain_tenant_bound(report_harness: ReportHarness):
    h = report_harness
    _, feedback_id = await reviewed(h)
    service, _ = services(h)
    source = OutcomeSourceCommand(source_type="TASK_ACTUALS", source_id=h.task_id, source_version=0)
    key = str(uuid4())
    first = await service.record(
        actor=h.actor, feedback_id=feedback_id, source=source, idempotency_key=key
    )
    replay = await service.record(
        actor=h.actor, feedback_id=feedback_id, source=source, idempotency_key=key
    )
    assert first.id == replay.id
    dedupe = await service.record(
        actor=h.actor, feedback_id=feedback_id, source=source, idempotency_key=str(uuid4())
    )
    assert first.id == dedupe.id
    with pytest.raises(ReportError, match="IDEMPOTENCY_KEY_REUSED"):
        await service.record(
            actor=h.actor,
            feedback_id=feedback_id,
            source=source.model_copy(update={"source_version": 1}),
            idempotency_key=key,
        )
    for actor in (h.employee, h.foreign):
        with pytest.raises(ReportError):
            await service.record(
                actor=actor, feedback_id=feedback_id, source=source, idempotency_key=str(uuid4())
            )
    with pytest.raises(ReportError):
        await service.record(
            actor=h.actor,
            feedback_id=feedback_id,
            source=source.model_copy(update={"source_id": uuid4()}),
            idempotency_key=str(uuid4()),
        )
    assert (
        await h.sql(
            "SELECT count(*) FROM feedback_outcomes WHERE feedback_id=:id", {"id": feedback_id}
        )
    ).scalar_one() == 1
    assert (
        await h.sql(
            "SELECT count(*) FROM audit_events WHERE organization_id=:org AND action='fe"
            "edback.outcome.recorded' AND outcome='REJECTED'",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 3


@pytest.mark.asyncio
async def test_concurrent_outcome_dedupe(report_harness: ReportHarness):
    import asyncio

    h = report_harness
    _, feedback_id = await reviewed(h)
    service, _ = services(h)
    source = OutcomeSourceCommand(source_type="TASK_ACTUALS", source_id=h.task_id, source_version=0)
    first, second = await asyncio.gather(
        *[
            service.record(
                actor=h.actor, feedback_id=feedback_id, source=source, idempotency_key=str(uuid4())
            )
            for _ in range(2)
        ]
    )
    assert first.id == second.id


@pytest.mark.asyncio
async def test_outcome_api_validation_rls_and_append_only(report_harness: ReportHarness):
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    h = report_harness
    r, feedback_id = await reviewed(h)
    body = {"source_type": "TASK_ACTUALS", "source_id": str(h.task_id), "source_version": 0}
    async with AsyncClient(transport=ASGITransport(app=h.app()), base_url="http://test") as client:
        for invalid in (
            {**body, "facts": {"to_status": "DONE"}},
            {**body, "organization_id": str(h.actor.organization_id)},
        ):
            response = await client.post(
                f"/api/v1/feedback/{feedback_id}/outcomes",
                json=invalid,
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert response.status_code == 422
        assert (
            await client.post(f"/api/v1/feedback/{feedback_id}/outcomes", json=body)
        ).status_code == 422
        result = await client.post(
            f"/api/v1/feedback/{feedback_id}/outcomes",
            json=body,
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert result.status_code == 201, result.text
        projection = await client.get(f"/api/v1/reports/{r.report.id}")
        assert projection.headers["Cache-Control"] == "private, no-store"
        assert projection.json()["feedback_outcomes"][0]["id"] == result.json()["id"]
        assert (
            await client.get("/api/v1/feedback/rates", params={"project_id": str(h.project_id)})
        ).json()["accept_percent"] == "100"
    for actor in (h.employee, h.foreign):
        async with AsyncClient(
            transport=ASGITransport(app=h.app(actor)), base_url="http://test"
        ) as client:
            assert (
                await client.post(
                    f"/api/v1/feedback/{feedback_id}/outcomes",
                    json=body,
                    headers={"Idempotency-Key": str(uuid4())},
                )
            ).status_code in (403, 404)
            assert (
                await client.get("/api/v1/feedback/rates", params={"project_id": str(h.project_id)})
            ).status_code in (403, 404)
        async with h.engine.connect() as connection, connection.begin():
            await connection.execute(text("SET LOCAL ROLE app_runtime"))
            await connection.execute(
                text(
                    "SELECT set_config('app.organization_id',:org,true),set_config('app.membershi"
                    "p_id',:member,true)"
                ),
                {"org": str(actor.organization_id), "member": str(actor.membership_id)},
            )
            assert (
                await connection.execute(text("SELECT count(*) FROM feedback_outcomes"))
            ).scalar_one() == 0
    for statement in (
        "UPDATE feedback_outcomes SET state='AVAILABLE' WHERE id=:id",
        "DELETE FROM feedback_outcomes WHERE id=:id",
    ):
        with pytest.raises(DBAPIError):
            await h.sql(statement, {"id": result.json()["id"]})
    _, other_feedback_id = await reviewed(h)
    with pytest.raises(DBAPIError) as cross:
        await h.sql(
            "INSERT INTO feedback_outcomes SELECT :id,organization_id,:feedback,:member,schema_ve"
            "rsion,source_type,source_id,source_version,task_transition_id,actual_task_id,observa"
            "tion_id,blocker_transition_id,state,facts,occurred_at,recorded_at FROM "
            "feedback_outcomes WHERE id=:original",
            {
                "id": uuid4(),
                "feedback": other_feedback_id,
                "member": h.foreign.membership_id,
                "original": result.json()["id"],
            },
        )
    assert getattr(cross.value.orig, "sqlstate", None) == "23503"
    assert (
        await h.sql(
            "SELECT count(*) FROM audit_events WHERE organization_id=:org AND "
            "action='feedback.outcome.recorded' AND outcome='REJECTED'",
            {"org": h.actor.organization_id},
        )
    ).scalar_one() == 4


@pytest.mark.asyncio
async def test_reported_100_is_not_completion_and_historical_outcome_survives_correction(
    report_harness: ReportHarness,
):
    h = report_harness
    _, feedback_id = await reviewed(h)
    service, _ = services(h)
    ids = {
        "draft": uuid4(),
        "update": uuid4(),
        "observation": uuid4(),
        "projection": uuid4(),
        "org": h.actor.organization_id,
        "task": h.task_id,
        "member": h.employee.membership_id,
    }
    await h.sql(
        "INSERT INTO daily_update_drafts(id,organization_id,owner_membership_id,version) VALUES "
        "(:draft,:org,:member,1)",
        ids,
    )
    await h.sql(
        "INSERT INTO daily_updates(id,organization_id,draft_id,owner_membership_id,draft_version,"
        "confirmed_at) VALUES (:update,:org,:draft,:member,1,now())",
        ids,
    )
    await h.sql(
        "INSERT INTO task_progress_observations(id,organization_id,update_id,task_id,owner_member"
        "ship_id,progress_version,reporting_date,reporting_at,confirmed_at,reported_percent,remai"
        "ning_hours,payload) VALUES "
        "(:observation,:org,:update,:task,:member,1,current_date,now(),now(),100,0,'{}')",
        ids,
    )
    await h.sql(
        "INSERT INTO "
        "task_actual_projections(id,organization_id,task_id,progress_version,observation_id) "
        "VALUES (:projection,:org,:task,1,:observation)",
        ids,
    )
    source = OutcomeSourceCommand(source_type="TASK_ACTUALS", source_id=h.task_id, source_version=1)
    first = await service.record(
        actor=h.actor, feedback_id=feedback_id, source=source, idempotency_key=str(uuid4())
    )
    assert first.state == "AVAILABLE"
    assert first.facts["reported_percent"] == "100.0000"
    assert first.facts["completion_state"] == "UNKNOWN"
    assert (
        await h.sql("SELECT status FROM tasks WHERE id=:id", {"id": h.task_id})
    ).scalar_one() == "IN_PROGRESS"
    await h.sql("UPDATE task_actual_projections SET progress_version=2 WHERE id=:projection", ids)
    with pytest.raises(ReportError, match="OUTCOME_SOURCE_INVALID"):
        await service.record(
            actor=h.actor,
            feedback_id=feedback_id,
            source=source.model_copy(update={"source_version": 2}),
            idempotency_key=str(uuid4()),
        )
    ids.update(
        {"old": ids["observation"], "observation": uuid4(), "draft": uuid4(), "update": uuid4()}
    )
    await h.sql(
        "INSERT INTO daily_update_drafts(id,organization_id,owner_membership_id,version) VALUES "
        "(:draft,:org,:member,1)",
        ids,
    )
    await h.sql(
        "INSERT INTO daily_updates(id,organization_id,draft_id,owner_membership_id,draft_version,"
        "confirmed_at) VALUES (:update,:org,:draft,:member,1,now())",
        ids,
    )
    await h.sql(
        "INSERT INTO task_progress_observations(id,organization_id,update_id,task_id,owner_member"
        "ship_id,progress_version,reporting_date,reporting_at,confirmed_at,reported_percent,remai"
        "ning_hours,payload,corrects_observation_id) VALUES "
        "(:observation,:org,:update,:task,:member,2,current_date,now(),now(),80,2,'{}',:old)",
        ids,
    )
    await h.sql(
        "UPDATE task_actual_projections SET progress_version=2,observation_id=:observation WHERE "
        "id=:projection",
        ids,
    )
    next_outcome = await service.record(
        actor=h.actor,
        feedback_id=feedback_id,
        source=source.model_copy(update={"source_version": 2}),
        idempotency_key=str(uuid4()),
    )
    assert next_outcome.id != first.id
    assert next_outcome.facts["reported_percent"] == "80.0000"
    replay = await service.record(
        actor=h.actor, feedback_id=feedback_id, source=source, idempotency_key=str(uuid4())
    )
    assert replay == first
    assert (
        await h.sql(
            "SELECT count(*) FROM feedback_outcomes WHERE feedback_id=:id", {"id": feedback_id}
        )
    ).scalar_one() == 2


@pytest.mark.asyncio
async def test_rates_use_terminal_generations_and_separate_fallbacks(report_harness: ReportHarness):
    from app.modules.feedback.domain.feedback import FeedbackCommand
    from app.modules.reporting.domain.commands import (
        CreateReportCommand,
        EditReportCommand,
        RejectReportCommand,
    )
    from tests.test_report_review_integration import worker
    from work_management_ai.agents.reporting.contracts import NarrativeTextBlock

    h = report_harness
    _, service = services(h)
    for _ in range(2):
        r = await ready(h)
        key = str(uuid4())
        for _ in range(2):
            await reviews(h).publish(
                actor=h.actor,
                report_id=r.report.id,
                command=publish_command(r),
                expected_version=r.report.version,
                idempotency_key=key,
            )
        await service.record(
            actor=h.actor,
            command=FeedbackCommand(
                report_id=r.report.id,
                report_version_id=r.selected_version.id,
                decision="REJECT",
                reason="Advisory only",
            ),
            idempotency_key=str(uuid4()),
        )
    r = await ready(h)
    assert r.selected_version.narrative
    edited = await reviews(h).edit(
        actor=h.actor,
        report_id=r.report.id,
        command=EditReportCommand(
            parent_version_id=r.selected_version.id,
            snapshot_hash=r.snapshot.snapshot_hash,
            narrative=r.selected_version.narrative.model_copy(
                update={
                    "blocks": (
                        *r.selected_version.narrative.blocks,
                        NarrativeTextBlock(
                            id="limitation",
                            section="limitations",
                            kind="LIMITATION",
                            text="Only captured data is included.",
                            source_refs=(),
                            assumptions=(),
                        ),
                    )
                }
            ),
        ),
        expected_version=r.report.version,
        idempotency_key=str(uuid4()),
    )
    assert await worker(h)
    checked = await h.service.get(actor=h.actor, report_id=edited.report.id)
    edit_result = await reviews(h).publish(
        actor=h.actor,
        report_id=r.report.id,
        command=publish_command(checked),
        expected_version=checked.report.version,
        idempotency_key=str(uuid4()),
    )
    assert edit_result.report_result.feedback[0].decision == "EDIT"
    r = await ready(h)
    await reviews(h).reject(
        actor=h.actor,
        report_id=r.report.id,
        command=RejectReportCommand(
            report_version_id=r.selected_version.id,
            snapshot_hash=r.snapshot.snapshot_hash,
            reason="Needs more context",
        ),
        expected_version=r.report.version,
        idempotency_key=str(uuid4()),
    )
    pending = await h.service.create(
        actor=h.actor,
        command=CreateReportCommand(project_id=h.project_id),
        idempotency_key=str(uuid4()),
    )
    failed = await h.service.create(
        actor=h.actor,
        command=CreateReportCommand(project_id=h.project_id),
        idempotency_key=str(uuid4()),
    )
    await h.sql(
        "UPDATE report_generation_jobs SET state='AI_UNAVAILABLE' WHERE id=:id",
        {"id": failed.generation_id},
    )
    await h.service.create(
        actor=h.actor,
        command=CreateReportCommand(project_id=h.project_id, narrative_enabled=False),
        idempotency_key=str(uuid4()),
    )
    rates = await service.rates(actor=h.actor, project_id=h.project_id)
    assert rates.reviewed_generation_count == 4
    assert (rates.accept_percent, rates.edit_percent, rates.reject_percent) == (50, 25, 25)
    assert (
        rates.pending_generation_count,
        rates.failed_generation_count,
        rates.manual_report_count,
    ) == (1, 1, 1)
    assert (await h.service.get(actor=h.actor, report_id=pending.report.id)).review_rates == rates


@pytest.mark.asyncio
async def test_blocker_resolution_has_verified_versioned_lifecycle_source(
    report_harness: ReportHarness,
):
    from datetime import UTC, datetime

    from app.modules.progress.domain.blockers import BlockerCommand, transition_blocker

    h = report_harness
    _, feedback_id = await reviewed(h)
    service, _ = services(h)
    at = datetime.now(UTC)
    blocker, _ = transition_blocker(
        None,
        BlockerCommand(
            task_id=h.task_id,
            expected_task_version=1,
            action="CREATE",
            text="Waiting for documents",
        ),
        h.actor.membership_id,
        at,
    )
    blocker, transition = transition_blocker(
        blocker,
        BlockerCommand(
            task_id=h.task_id,
            expected_task_version=1,
            action="RESOLVE",
            blocker_id=blocker.id,
            expected_blocker_version=1,
        ),
        h.actor.membership_id,
        at,
    )
    values = {
        "id": blocker.id,
        "org": h.actor.organization_id,
        "task": h.task_id,
        "member": h.actor.membership_id,
        "payload": blocker.model_dump_json(),
        "transition": transition.id,
        "lifecycle": transition.model_dump_json(),
        "at": at,
    }
    await h.sql(
        "INSERT INTO blockers(id,organization_id,task_id,created_by_membership_id,version,severi"
        "ty,status,archived,payload,created_at,updated_at) VALUES "
        "(:id,:org,:task,:member,2,'MEDIUM','RESOLVED',false,CAST(:payload AS jsonb),:at,:at)",
        values,
    )
    await h.sql(
        "INSERT INTO blocker_transitions(id,organization_id,blocker_id,version,actor_membership_"
        "id,payload,at) VALUES (:transition,:org,:id,2,:member,CAST(:lifecycle AS jsonb),:at)",
        values,
    )
    source = OutcomeSourceCommand(
        source_type="BLOCKER_RESOLUTION", source_id=transition.id, source_version=2
    )
    result = await service.record(
        actor=h.actor, feedback_id=feedback_id, source=source, idempotency_key=str(uuid4())
    )
    assert result.state == "AVAILABLE"
    assert result.facts["status"] == "RESOLVED"
    assert result.occurred_at == transition.at
    with pytest.raises(ReportError, match="OUTCOME_SOURCE_STALE"):
        await service.record(
            actor=h.actor,
            feedback_id=feedback_id,
            source=source.model_copy(update={"source_version": 3}),
            idempotency_key=str(uuid4()),
        )


@pytest.mark.asyncio
async def test_outcome_source_move_and_revoked_identity_are_denied_on_read_and_replay(
    report_harness: ReportHarness,
):
    h = report_harness
    r, feedback_id = await reviewed(h)
    service, _ = services(h)
    source = OutcomeSourceCommand(source_type="TASK_ACTUALS", source_id=h.task_id, source_version=0)
    key = str(uuid4())
    result = await service.record(
        actor=h.actor, feedback_id=feedback_id, source=source, idempotency_key=key
    )
    assert result.state == "UNKNOWN"
    other_project = uuid4()
    values = {
        "id": other_project,
        "org": h.actor.organization_id,
        "member": h.actor.membership_id,
        "task": h.task_id,
    }
    await h.sql(
        "INSERT INTO "
        "projects(id,organization_id,name,created_by_membership_id,updated_by_membership_id) "
        "VALUES (:id,:org,'Other scope',:member,:member)",
        values,
    )
    await h.sql("UPDATE tasks SET project_id=:id WHERE id=:task", values)
    assert (await h.service.get(actor=h.actor, report_id=r.report.id)).feedback_outcomes == ()
    with pytest.raises(ReportError, match="RESOURCE_NOT_FOUND"):
        await service.record(
            actor=h.actor, feedback_id=feedback_id, source=source, idempotency_key=key
        )
    await h.sql("UPDATE memberships SET is_active=false WHERE id=:member", values)
    with pytest.raises(ReportError, match="FORBIDDEN"):
        await service.record(
            actor=h.actor, feedback_id=feedback_id, source=source, idempotency_key=key
        )


@pytest.mark.asyncio
async def test_cached_publication_replay_rechecks_outcome_source_access(
    report_harness: ReportHarness,
):
    from app.modules.feedback.domain.feedback import FeedbackCommand

    h = report_harness
    r = await ready(h)
    outcomes, feedback = services(h)
    advisory = await feedback.record(
        actor=h.actor,
        command=FeedbackCommand(
            report_id=r.report.id,
            report_version_id=r.selected_version.id,
            decision="ACCEPT",
            reason="Observed facts",
        ),
        idempotency_key=str(uuid4()),
    )
    await outcomes.record(
        actor=h.actor,
        feedback_id=advisory.feedback.id,
        source=OutcomeSourceCommand(
            source_type="TASK_ACTUALS", source_id=h.task_id, source_version=0
        ),
        idempotency_key=str(uuid4()),
    )
    key = str(uuid4())
    first = await reviews(h).publish(
        actor=h.actor,
        report_id=r.report.id,
        command=publish_command(r),
        expected_version=r.report.version,
        idempotency_key=key,
    )
    assert len(first.report_result.feedback_outcomes) == 1
    other = uuid4()
    await h.sql(
        "INSERT INTO "
        "projects(id,organization_id,name,created_by_membership_id,updated_by_membership_id) "
        "VALUES (:id,:org,'Other scope',:member,:member)",
        {"id": other, "org": h.actor.organization_id, "member": h.actor.membership_id},
    )
    await h.sql(
        "UPDATE tasks SET project_id=:project WHERE id=:task", {"project": other, "task": h.task_id}
    )
    replay = await reviews(h).publish(
        actor=h.actor,
        report_id=r.report.id,
        command=publish_command(r),
        expected_version=r.report.version,
        idempotency_key=key,
    )
    assert replay.report_result.replayed
    assert replay.report_result.feedback_outcomes == ()


@pytest.mark.asyncio
async def test_cli_resolves_current_identity_instead_of_trusting_ids(
    report_harness: ReportHarness, capsys: pytest.CaptureFixture[str]
):
    import argparse
    import json

    from app.scripts.record_report_outcome import record

    h = report_harness
    _, feedback_id = await reviewed(h)
    args = argparse.Namespace(
        organization_id=h.actor.organization_id,
        membership_id=h.actor.membership_id,
        feedback_id=feedback_id,
        source_type="TASK_ACTUALS",
        source_id=h.task_id,
        source_version=0,
        idempotency_key=str(uuid4()),
    )
    await record(args)
    assert json.loads(capsys.readouterr().out)["state"] == "UNKNOWN"
    args.membership_id = h.employee.membership_id
    args.idempotency_key = str(uuid4())
    with pytest.raises(ReportError, match="FORBIDDEN"):
        await record(args)
