"""Evaluate one immutable dataset as a configured Admin; hosted requires explicit policy/budget."""

import argparse
import asyncio
from uuid import UUID

from app.core.config import get_settings
from app.core.database import create_database_engine, create_session_factory
from app.modules.feedback.adapters.evaluation_runner import (
    EvaluationRunner,
    FrozenDatasetReader,
    HostedReportingEvaluationGateway,
    ReportingEvaluationProvider,
    hosted_policy,
)
from app.modules.feedback.adapters.transaction import CurationTransactions
from app.modules.identity.adapters.auth_repository import SqlAlchemyAuthTransactionFactory
from app.modules.identity.application.current_actor_service import CurrentActorService
from app.modules.reporting.domain.reports import ReportError


async def evaluate(args: argparse.Namespace) -> bool:
    settings = get_settings()
    engine = create_database_engine(settings)
    try:
        sessions = create_session_factory(engine)
        actor = await CurrentActorService(SqlAlchemyAuthTransactionFactory(sessions)).resolve(
            organization_id=args.organization_id, membership_id=args.membership_id
        )
        if actor is None:
            raise ReportError("FORBIDDEN", 403)
        reader = FrozenDatasetReader(CurationTransactions(sessions, settings.reporting_timezone))
        # Load and authorize before constructing a hosted provider or reaching transport.
        await reader.load(actor, args.dataset_version)
        if args.provider == "hosted":
            hosted_policy(settings, budget_tokens=args.budget_tokens)

            async def authorize() -> None:
                await reader.load(actor, args.dataset_version)

            provider = ReportingEvaluationProvider(
                HostedReportingEvaluationGateway(
                    settings,
                    budget_tokens=args.budget_tokens,
                    authorize=authorize,
                    judge=args.judge,
                )
            )
        else:
            if args.judge:
                raise ReportError("EVALUATION_HOSTED_CONFIGURATION_REQUIRED", 422)
            provider = ReportingEvaluationProvider()
        result = await EvaluationRunner(reader, provider).run(
            actor=actor, dataset_version_id=args.dataset_version
        )
        print(result.model_dump_json())
        return result.gate_passed
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-version", type=UUID, required=True)
    parser.add_argument("--organization-id", type=UUID, required=True)
    parser.add_argument("--membership-id", type=UUID, required=True)
    parser.add_argument("--provider", choices=("mock", "hosted"), default="mock")
    parser.add_argument("--budget-tokens", type=int)
    parser.add_argument(
        "--judge", action="store_true", help="Opt in to advisory hosted rubric scoring"
    )
    try:
        passed = asyncio.run(evaluate(parser.parse_args()))
    except ReportError as exc:
        parser.exit(1, f"{exc.code}\n")
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
