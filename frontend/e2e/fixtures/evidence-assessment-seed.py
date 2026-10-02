"""Deterministic comparison fixture, strictly limited to the browser-test database."""
import asyncio
import sys
from uuid import UUID, uuid4
from sqlalchemy import text
from app.core.config import Settings
from app.core.database import create_database_engine, create_session_factory
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.progress.adapters.assessment_repository import SqlAlchemyAssessmentTransactions
from app.modules.progress.application.assessment_service import AssessmentService, EvidenceComparisonResult
from app.modules.progress.domain.daily_updates import SelectedEvidence
from app.modules.progress.domain.evidence_support import ClaimFinding


class UnsupportedFixture:
    async def compare(self, claims, original_sources, budget):
        return EvidenceComparisonResult(findings=tuple(ClaimFinding(claim_id=c.id,finding="UNSUPPORTED",source_refs=c.evidence_refs) for c in claims),processed_sources=tuple(SelectedEvidence(evidence_id=s.evidence_id,version=s.version) for s in original_sources))


async def main():
    settings=Settings()
    if not settings.database_url.endswith('/work_management_e2e'):
        raise RuntimeError('Browser fixtures require the dedicated E2E database')
    draft_id=UUID(sys.argv[1])
    engine=create_database_engine(settings)
    try:
        async with engine.connect() as connection:
            row=(await connection.execute(text("SELECT u.id,u.email_normalized,u.display_name,m.id,m.organization_id,o.name,m.role,d.version FROM daily_update_drafts d JOIN memberships m ON m.id=d.owner_membership_id AND m.organization_id=d.organization_id JOIN users u ON u.id=m.user_id JOIN organizations o ON o.id=m.organization_id WHERE d.id=:id"),{"id":draft_id})).one()
        actor=AuthenticatedActor(row[0],row[1],row[2],row[3],row[4],row[5],MembershipRole(row[6]))
        service=AssessmentService(SqlAlchemyAssessmentTransactions(create_session_factory(engine)),UnsupportedFixture())
        await service.assess(actor,draft_id,row[7],str(uuid4()),'browser-warning-fixture')
    finally:
        await engine.dispose()


asyncio.run(main())
