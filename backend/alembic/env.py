"""Alembic runtime configured for the application's async PostgreSQL engine."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.core.database import Base, create_database_engine
from app.modules.assistant.adapters import daily_update_usage_models
from app.modules.assistant.adapters import database_models as assistant_models
from app.modules.audit.adapters import database_models as audit_models
from app.modules.identity.adapters import database_models as identity_models
from app.modules.organization.adapters import database_models as organization_models
from app.modules.people_capacity.adapters import database_models as people_capacity_models
from app.modules.planning_runs.adapters import database_models as planning_runs_models
from app.modules.progress.adapters import (
    assessment_models,
    blocker_models,  # noqa: F401
    completion_models,
    daily_update_models,
    evidence_models,
    extraction_models,
    progress_models,
)
from app.modules.risk.adapters import database_models as risk_models
from app.modules.work.adapters import database_models as work_models
from app.modules.work.planning.adapters import database_models as planning_models
from app.modules.work.planning.assignment.adapters import database_models as team_requirement_models
from app.modules.work.planning.assignment.adapters import recommendation_models

_MODEL_MODULES = (
    risk_models,
    daily_update_usage_models,
    assessment_models,
    completion_models,
    daily_update_models,
    evidence_models,
    extraction_models,
    progress_models,
    audit_models,
    assistant_models,
    identity_models,
    organization_models,
    work_models,
    planning_models,
    planning_runs_models,
    people_capacity_models,
    team_requirement_models,
    recommendation_models,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Render migration SQL without opening a database connection."""

    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run synchronous Alembic operations through an async connection."""

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Open the migration connection and always release its pool."""

    connectable: AsyncEngine = create_database_engine()
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    """Bridge Alembic's synchronous command entrypoint to asyncio."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
