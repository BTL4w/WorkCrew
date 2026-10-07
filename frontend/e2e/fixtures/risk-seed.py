"""Persist a risk through the real service, limited to the dedicated browser database."""

import asyncio
import sys
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import text

from app.core.config import Settings
from app.core.database import create_database_engine, create_session_factory
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.risk.adapters.repository import RiskTransactions, input_hash
from app.modules.risk.application.risk_service import RiskService
from app.modules.risk.domain.assessments import RiskInputs, RiskJudgment


class Model:
    async def assess(self, inputs: RiskInputs):
        return RiskJudgment.model_validate(
            {
                "score": "83",
                "rationale": "Cần kiểm tra hạn hoàn thành và blocker.",
                "observations": [
                    {"text": "Hạn giao cần được xem lại.", "source_ids": [inputs.facts[0].id]}
                ],
                "limitations": [],
                "recommendations": ["Trao đổi lại lịch giao với người phụ trách."],
            }
        ), "mock:browser-risk"


async def main():
    settings = Settings()
    if not settings.database_url.endswith("/work_management_e2e"):
        raise RuntimeError("Browser fixture requires the dedicated E2E database")
    task = UUID(sys.argv[1])
    engine = create_database_engine(settings)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT u.id,u.email_normalized,u.display_name,m.id,"
                        "m.organization_id,o.name,m.role "
                        "FROM tasks t JOIN memberships m ON m.organization_id=t.organization_id "
                        "JOIN users u ON u.id=m.user_id JOIN organizations o ON "
                        "o.id=m.organization_id "
                        "WHERE t.id=:id AND u.email_normalized='manager@example.test'"
                    ),
                    {"id": task},
                )
            ).one()
        actor = AuthenticatedActor(
            row[0], row[1], row[2], row[3], row[4], row[5], MembershipRole(row[6])
        )
        service = RiskService(
            RiskTransactions(create_session_factory(engine), settings.reporting_timezone), Model()
        )
        # The native reconciliation job may otherwise finish AFTER the synthetic
        # refresh and replace the current result with the default mock's null score.
        # Wait for that exact production cause to settle (null scores end FAILED)
        # and queued triggers, without changing worker policy, fixture score
        # or business inputs to force a ready state.
        async with service.transactions(actor) as repo:
            inputs = await repo.inputs(task)
        cause = uuid5(NAMESPACE_URL, f"risk:{actor.organization_id}:{task}:{input_hash(inputs)}")
        for _ in range(120):
            async with engine.connect() as c:
                state = await c.scalar(
                    text(
                        "SELECT state FROM risk_refresh_jobs WHERE organization_id=:org "
                        "AND task_id=:task AND cause_id=:cause"
                    ),
                    {"org": actor.organization_id, "task": task, "cause": cause},
                )
                pending = await c.scalar(
                    text(
                        "SELECT count(*) FROM risk_refresh_jobs WHERE organization_id=:org "
                        "AND task_id=:task AND state IN ('PENDING','RUNNING')"
                    ),
                    {"org": actor.organization_id, "task": task},
                )
                triggers = await c.scalar(
                    text(
                        "SELECT count(*) FROM outbox_events e WHERE e.organization_id=:org "
                        "AND e.status IN ('PENDING','DISPATCHING') AND "
                        "((e.event_type='weekly_plan.captured' AND e.payload->>'project_week_id'="
                        "(SELECT project_week_id::text FROM tasks WHERE id=:task)) OR "
                        "(e.event_type='daily_update.confirmed' AND e.payload->'task_ids' "
                        "@>jsonb_build_array(CAST(:task AS text))) OR "
                        "(e.event_type IN ('task.assigned.v1','blocker.changed.v1') "
                        "AND e.payload->>'task_id'=CAST(:task AS text)))"
                    ),
                    {"org": actor.organization_id, "task": task},
                )
            if state in ("DONE", "FAILED") and not pending and not triggers:
                break
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError("Native risk reconciliation did not settle for browser fixture")
        # Consume any older jobs first; every call remains bounded and uses normal leases.
        for _ in range(200):
            if not await service.run_once(actor):
                break
        await service.refresh(actor, task, uuid4())
        for _ in range(200):
            await service.run_once(actor)
            result = await service.current(actor, task)
            if result and result.state == "READY":
                return
        raise RuntimeError("Browser risk fixture did not finish")
    finally:
        await engine.dispose()


asyncio.run(main())
