"""Composition helpers for the local authentication adapters."""

from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.core.database import create_database_engine, create_session_factory
from app.modules.identity.adapters.auth_repository import SqlAlchemyAuthTransactionFactory
from app.modules.identity.adapters.security import (
    Argon2PasswordVerifier,
    OpaqueSessionTokenCodec,
)
from app.modules.identity.application.auth_service import AuthService
from app.modules.identity.application.ports import LoginThrottlePort


def create_auth_runtime(
    settings: Settings, *, login_throttle: LoginThrottlePort | None = None
) -> tuple[AuthService, AsyncEngine]:
    """Compose the auth use cases with PostgreSQL and local security adapters."""

    if settings.login_throttle_enabled and login_throttle is None:
        raise ValueError("LOGIN_THROTTLE_REQUIRED")

    engine = create_database_engine(settings)
    transaction_factory = SqlAlchemyAuthTransactionFactory(create_session_factory(engine))
    service = AuthService(
        login_throttle=login_throttle,
        transaction_factory=transaction_factory,
        password_verifier=Argon2PasswordVerifier(),
        token_codec=OpaqueSessionTokenCodec(),
        organization_slug=settings.local_auth_organization_slug,
        session_ttl_seconds=settings.session_ttl_seconds,
    )
    return service, engine
