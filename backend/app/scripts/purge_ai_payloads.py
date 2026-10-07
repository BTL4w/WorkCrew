"""Trusted tenant allowlist cleanup. Dry-run outputs counts only."""

import argparse
import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import create_database_engine, create_session_factory
from app.modules.feedback.adapters.payload_repository import RetentionTransactions
from app.modules.feedback.application.retention_service import RetentionService


async def run(organization_id: UUID, *, dry_run: bool, limit: int) -> None:
    settings = get_settings()
    if str(organization_id) not in {str(value) for value in settings.worker_organization_ids}:
        raise ValueError("ORGANIZATION_NOT_IN_TRUSTED_WORKER_ALLOWLIST")
    engine = create_database_engine(settings)
    try:
        sessions = create_session_factory(engine)
        if dry_run:
            async with RetentionTransactions(sessions)(organization_id) as repo:
                count = await repo.session.scalar(
                    text(
                        "SELECT count(*) FROM ai_retention_payloads WHERE organization_id=:org "
                        "AND purged_at IS NULL AND "
                        "LEAST(expires_at,created_at+make_interval(days=>:days))<=:now"
                    ),
                    {
                        "org": organization_id,
                        "now": datetime.now(UTC),
                        "days": settings.ai_raw_context_retention_days,
                    },
                )
                print(f"expired_payloads={count}")
        else:
            result = await RetentionService(RetentionTransactions(sessions)).purge_once(
                organization_id=organization_id, now=datetime.now(UTC), limit=limit
            )
            print(f"purged_payloads={result.purged}")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("organization_id", type=UUID)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=100, choices=range(1, 101))
    args = parser.parse_args()
    asyncio.run(run(args.organization_id, dry_run=args.dry_run, limit=args.limit))


if __name__ == "__main__":
    main()
