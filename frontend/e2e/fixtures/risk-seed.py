"""Persist a risk through the real service, limited to the dedicated browser database."""
import asyncio
import sys
from uuid import UUID, uuid4
from sqlalchemy import text
from app.core.config import Settings
from app.core.database import create_database_engine, create_session_factory
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.risk.adapters.repository import RiskTransactions
from app.modules.risk.application.risk_service import RiskService
from app.modules.risk.domain.assessments import RiskInputs, RiskJudgment


class Model:
    async def assess(self, inputs: RiskInputs):
        return RiskJudgment.model_validate({
            "score": "83", "rationale": "Cần kiểm tra hạn hoàn thành và blocker.",
            "observations": [{"text": "Hạn giao cần được xem lại.", "source_ids": [inputs.facts[0].id]}],
            "limitations": [], "recommendations": ["Trao đổi lại lịch giao với người phụ trách."]
        }), "mock:browser-risk"


async def main():
    settings = Settings()
    if not settings.database_url.endswith('/work_management_e2e'):
        raise RuntimeError('Browser fixture requires the dedicated E2E database')
    task = UUID(sys.argv[1])
    engine = create_database_engine(settings)
    try:
        async with engine.connect() as connection:
            row = (await connection.execute(text(
                "SELECT u.id,u.email_normalized,u.display_name,m.id,m.organization_id,o.name,m.role "
                "FROM tasks t JOIN memberships m ON m.organization_id=t.organization_id "
                "JOIN users u ON u.id=m.user_id JOIN organizations o ON o.id=m.organization_id "
                "WHERE t.id=:id AND u.email_normalized='manager@example.test'"
            ), {'id': task})).one()
        actor = AuthenticatedActor(row[0],row[1],row[2],row[3],row[4],row[5],MembershipRole(row[6]))
        service = RiskService(RiskTransactions(create_session_factory(engine), settings.reporting_timezone), Model())
        # Consume any older jobs first; every call remains bounded and uses normal leases.
        for _ in range(200):
            if not await service.run_once(actor):
                break
        await service.refresh(actor, task, uuid4())
        for _ in range(200):
            await service.run_once(actor)
            result = await service.current(actor, task)
            if result and result.state == 'READY':
                return
        raise RuntimeError('Browser risk fixture did not finish')
    finally:
        await engine.dispose()


asyncio.run(main())
