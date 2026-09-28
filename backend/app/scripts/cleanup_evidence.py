"""Operator-only idempotent cleanup of expired unconfirmed original uploads."""

import argparse
import asyncio
from uuid import UUID

from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.core.database import create_database_engine, create_session_factory
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.progress.adapters.evidence_repository import SqlAlchemyEvidenceTransactionFactory
from app.modules.progress.adapters.filesystem_storage import FilesystemEvidenceStorage
from app.modules.progress.application.evidence_service import EvidenceService


async def cleanup_organization(organization_id: UUID, settings: Settings) -> int:
    """Tenant comes from trusted operator configuration, never a product request."""
    engine = create_database_engine(settings)
    sessions = create_session_factory(engine)
    service = EvidenceService(
        SqlAlchemyEvidenceTransactionFactory(sessions),
        FilesystemEvidenceStorage(settings.evidence_storage_root),
    )
    count = 0
    try:
        async with sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text("SELECT set_config('app.organization_id', :org, true)"),
                {"org": str(organization_id)},
            )
            result = await session.execute(
                text(
                    "SELECT m.id, m.user_id, m.role, u.email_display, "
                    "u.display_name, o.name FROM memberships m JOIN users u ON u.id=m.user_id "
                    "JOIN organizations o ON o.id=m.organization_id WHERE m.organization_id=:org"
                ),
                {"org": organization_id},
            )
            members = list(result.mappings())
        for member in members:
            actor = AuthenticatedActor(
                user_id=UUID(str(member["user_id"])),
                membership_id=UUID(str(member["id"])),
                organization_id=organization_id,
                role=MembershipRole(str(member["role"])),
                email=str(member["email_display"]),
                display_name=str(member["display_name"]),
                organization_name=str(member["name"]),
            )
            while deleted := await service.cleanup_expired(actor):
                count += deleted
        return count
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization-id", type=UUID, required=True)
    args = parser.parse_args()
    count = asyncio.run(cleanup_organization(args.organization_id, get_settings()))
    print(f"Expired unconfirmed originals cleaned: {count}")


if __name__ == "__main__":
    main()
