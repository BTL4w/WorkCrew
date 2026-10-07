"""Record one sourced outcome as a currently authorized identity; IDs never grant access."""

import argparse
import asyncio
from uuid import UUID

from app.core.config import get_settings
from app.core.database import create_database_engine, create_session_factory
from app.modules.feedback.adapters.transaction import FeedbackTransactions
from app.modules.feedback.application.outcome_service import OutcomeService
from app.modules.feedback.domain.outcomes import OutcomeSourceCommand
from app.modules.identity.adapters.auth_repository import SqlAlchemyAuthTransactionFactory
from app.modules.identity.application.current_actor_service import CurrentActorService
from app.modules.reporting.domain.reports import ReportError


async def record(args: argparse.Namespace) -> None:
    settings = get_settings()
    engine = create_database_engine(settings)
    try:
        sessions = create_session_factory(engine)
        actor = await CurrentActorService(SqlAlchemyAuthTransactionFactory(sessions)).resolve(
            organization_id=args.organization_id, membership_id=args.membership_id
        )
        if actor is None:
            raise ReportError("FORBIDDEN", 403)
        result = await OutcomeService(
            FeedbackTransactions(sessions, settings.reporting_timezone)
        ).record(
            actor=actor,
            feedback_id=args.feedback_id,
            source=OutcomeSourceCommand(
                source_type=args.source_type,
                source_id=args.source_id,
                source_version=args.source_version,
            ),
            idempotency_key=args.idempotency_key,
        )
        print(result.model_dump_json())
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("organization-id", "membership-id", "feedback-id", "source-id"):
        parser.add_argument("--" + field, type=UUID, required=True)
    parser.add_argument(
        "--source-type",
        choices=("TASK_TRANSITION", "TASK_ACTUALS", "BLOCKER_RESOLUTION"),
        required=True,
    )
    parser.add_argument("--source-version", type=int, required=True)
    parser.add_argument("--idempotency-key", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(record(args))
    except ReportError as exc:
        parser.exit(1, f"{exc.code}\n")


if __name__ == "__main__":
    main()
