"""Unit tests for the PostgreSQL persistence foundation."""

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import (
    NAMING_CONVENTION,
    Base,
    create_database_engine,
    create_session_factory,
)


def test_settings_require_selected_async_postgresql_driver() -> None:
    with pytest.raises(ValidationError, match="postgresql\\+psycopg"):
        Settings(database_url="sqlite+aiosqlite:///:memory:")


@pytest.mark.asyncio
async def test_engine_and_session_factory_do_not_connect_eagerly() -> None:
    settings = Settings(
        environment="test",
        database_url="postgresql+psycopg://user:password@localhost:5432/test_database",
    )
    engine = create_database_engine(settings)

    try:
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "psycopg"

        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            assert isinstance(session, AsyncSession)
            assert session.sync_session.autoflush is False
            assert session.sync_session.expire_on_commit is False
    finally:
        await engine.dispose()


def test_metadata_has_stable_constraint_naming_convention() -> None:
    assert Base.metadata.naming_convention == NAMING_CONVENTION
    assert NAMING_CONVENTION["pk"] == "pk_%(table_name)s"
    assert NAMING_CONVENTION["fk"].startswith("fk_%(table_name)s")


def test_alembic_environment_has_one_weekly_baselines_head() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(backend_root / "alembic.ini")
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["0023"]
    assert scripts.get_bases() == ["0001"]
