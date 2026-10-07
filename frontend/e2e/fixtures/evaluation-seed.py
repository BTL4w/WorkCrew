"""Synthetic reviewed dataset fixture through application services, never a product endpoint."""

import asyncio
import sys
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.core.database import create_database_engine
from app.modules.feedback.adapters.transaction import CurationTransactions
from app.modules.feedback.application.curation_service import CurationService
from app.modules.feedback.domain.evaluation import DatasetCommand
from app.modules.identity.adapters.auth_repository import SqlAlchemyAuthTransactionFactory
from app.modules.identity.application.current_actor_service import CurrentActorService
from app.modules.reporting.adapters.transaction import ReportTransactions
from app.modules.reporting.application.report_service import ReportService
from app.modules.reporting.domain.commands import CreateReportCommand
from tests.test_evaluation_curation_integration import command
from tests.test_report_api_integration import ReportHarness
from tests.test_report_review_integration import publish_command, reviews, worker


async def main():
    settings = Settings()
    if not settings.database_url.endswith("/work_management_e2e"):
        raise RuntimeError("Browser fixture requires the dedicated E2E database")
    if len(sys.argv) > 1:
        from uuid import UUID

        feedback_id = UUID(sys.argv[1])
        engine = create_database_engine(settings)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.connect() as conn:
                member = (
                    await conn.execute(
                        text(
                            "SELECT m.organization_id,m.id FROM memberships m JOIN users u ON "
                            "u.id=m.user_id WHERE u.email_normalized='admin@example.test'"
                        )
                    )
                ).one()
            actor = await CurrentActorService(SqlAlchemyAuthTransactionFactory(sessions)).resolve(
                organization_id=member.organization_id, membership_id=member.id
            )
            assert actor is not None
            curation = CurationService(CurationTransactions(sessions, settings.reporting_timezone))
            candidate = await curation.prepare(
                actor=actor, feedback_id=feedback_id, idempotency_key=str(uuid4())
            )
            for version, locale in enumerate(("vi", "en"), 1):
                await curation.curate(
                    actor=actor,
                    command=command(candidate, locale=locale),
                    expected_version=version,
                    idempotency_key=str(uuid4()),
                )
            dataset = await curation.freeze_dataset(
                actor=actor,
                command=DatasetCommand(name=f"closure-{uuid4().hex[:12]}", split="GOLDEN"),
                idempotency_key=str(uuid4()),
            )
            print(dataset.id)
        finally:
            await engine.dispose()
        return
    engine = create_database_engine(settings)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        member = (
            await conn.execute(
                text(
                    "SELECT m.organization_id,m.id FROM memberships m JOIN users u ON "
                    "u.id=m.user_id WHERE u.email_normalized='admin@example.test'"
                )
            )
        ).one()
    actor = await CurrentActorService(SqlAlchemyAuthTransactionFactory(sessions)).resolve(
        organization_id=member.organization_id, membership_id=member.id
    )
    assert actor is not None
    project, task = uuid4(), uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO "
                "projects(id,organization_id,name,created_by_membership_id,updated_by_membershi"
                "p_id) VALUES(:id,:org,'Synthetic evaluation',:member,:member)"
            ),
            {"id": project, "org": actor.organization_id, "member": actor.membership_id},
        )
        await conn.execute(
            text(
                "INSERT INTO "
                "tasks(id,organization_id,project_id,title,status,estimated_effort_hours,requir"
                "ed_skill_labels,created_by_membership_id,updated_by_membership_id) "
                "VALUES(:id,:org,:project,'Synthetic task','IN_PROGRESS',4,'[]',:member,:member)"
            ),
            {
                "id": task,
                "org": actor.organization_id,
                "project": project,
                "member": actor.membership_id,
            },
        )
    service = ReportService(ReportTransactions(sessions, "UTC"))
    h = ReportHarness(engine, service, actor, actor, actor, project, task)
    report = await service.create(
        actor=actor,
        command=CreateReportCommand(project_id=project, locale="en"),
        idempotency_key=str(uuid4()),
    )
    for _ in range(60):
        await worker(h)
        value = await service.get(actor=actor, report_id=report.report.id)
        if value.selected_version.origin == "AI_PROPOSED":
            break
        await asyncio.sleep(0.5)
    assert value.selected_version.origin == "AI_PROPOSED"
    publication = await reviews(h).publish(
        actor=actor,
        report_id=report.report.id,
        command=publish_command(value),
        expected_version=value.report.version,
        idempotency_key=str(uuid4()),
    )
    assert publication.terminal_outcome_id is not None
    curation = CurationService(CurationTransactions(sessions, "UTC"))
    candidate = await curation.prepare(
        actor=actor, feedback_id=publication.terminal_outcome_id, idempotency_key=str(uuid4())
    )
    for version, locale in enumerate(("vi", "en"), 1):
        await curation.curate(
            actor=actor,
            command=command(candidate, locale=locale),
            expected_version=version,
            idempotency_key=str(uuid4()),
        )
    dataset = await curation.freeze_dataset(
        actor=actor,
        command=DatasetCommand(name=f"evaluation-{uuid4().hex[:12]}", split="GOLDEN"),
        idempotency_key=str(uuid4()),
    )
    print(dataset.id)
    await engine.dispose()


asyncio.run(main())
