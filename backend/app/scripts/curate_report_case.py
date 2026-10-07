"""Prepare, human-curate and freeze numeric report cases using a configured current Admin."""

import argparse
import asyncio
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from app.core.config import get_settings
from app.core.database import create_database_engine, create_session_factory
from app.modules.feedback.adapters.transaction import CurationTransactions
from app.modules.feedback.application.curation_service import CurationService
from app.modules.feedback.domain.evaluation import CurateCaseCommand, DatasetCommand
from app.modules.identity.adapters.auth_repository import SqlAlchemyAuthTransactionFactory
from app.modules.identity.application.current_actor_service import CurrentActorService
from app.modules.reporting.domain.reports import ReportError


def read_command(path: Path) -> bytes:
    with path.open("rb") as source:
        return source.read(131073)


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    engine = create_database_engine(settings)
    try:
        sessions = create_session_factory(engine)
        actor = await CurrentActorService(SqlAlchemyAuthTransactionFactory(sessions)).resolve(
            organization_id=args.organization_id, membership_id=args.membership_id
        )
        if actor is None:
            raise ReportError("FORBIDDEN", 403)
        service = CurationService(CurationTransactions(sessions, settings.reporting_timezone))
        if args.action == "prepare":
            result = await service.prepare(
                actor=actor, feedback_id=args.feedback_id, idempotency_key=args.idempotency_key
            )
        else:
            # Trusted developer/Admin files only; bounded input and strict typed schemas.
            data = await asyncio.to_thread(read_command, Path(args.command_file))
            if len(data) > 131072:
                raise ReportError("VALIDATION_FAILED", 422)
            if args.action == "curate":
                command = CurateCaseCommand.model_validate_json(data)
                if getattr(args, "preview", False):
                    preview = await service.preview(
                        actor=actor, command=command, expected_version=args.expected_version
                    )
                    print(preview.model_dump_json())
                    return
                if not args.approve_reviewed:
                    command = command.model_copy(update={"permission_reviewed": False})
                result = await service.curate(
                    actor=actor,
                    command=command,
                    expected_version=args.expected_version,
                    idempotency_key=args.idempotency_key,
                )
            else:
                result = await service.freeze_dataset(
                    actor=actor,
                    command=DatasetCommand.model_validate_json(data),
                    idempotency_key=args.idempotency_key,
                )
        print(result.model_dump_json())
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization-id", type=UUID, required=True)
    parser.add_argument("--membership-id", type=UUID, required=True)
    parser.add_argument("--idempotency-key", required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    prepare = actions.add_parser("prepare")
    prepare.add_argument("--feedback-id", type=UUID, required=True)
    curate = actions.add_parser("curate")
    curate.add_argument("--command-file", type=Path, required=True)
    curate.add_argument("--expected-version", type=int, required=True)
    curate.add_argument("--approve-reviewed", action="store_true")
    curate.add_argument(
        "--preview",
        action="store_true",
        help="Show current numeric source and proposed fixture without writing",
    )
    freeze = actions.add_parser("freeze")
    freeze.add_argument("--command-file", type=Path, required=True)
    try:
        asyncio.run(run(parser.parse_args()))
    except ReportError as exc:
        parser.exit(1, f"{exc.code}\n")
    except (ValidationError, OSError):
        # Never print rejected payload contents or file paths into a shared log.
        parser.exit(1, "VALIDATION_FAILED\n")


if __name__ == "__main__":
    main()
