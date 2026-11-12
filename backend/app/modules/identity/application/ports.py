"""Typed ports owned by the authentication application layer."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor, LoginIdentity
from app.modules.optimization.application.admission import AdmissionDecision


class LoginThrottlePort(Protocol):
    async def check(
        self, *, trusted_organization_id: UUID, normalized_account: str, client_ip: str
    ) -> AdmissionDecision: ...


class PasswordVerifier(Protocol):
    """Verify a password without exposing provider-specific hash APIs."""

    def verify(self, password: str, encoded_hash: str | None) -> bool: ...


class SessionTokenCodec(Protocol):
    """Issue, hash, encode, and parse opaque session credentials."""

    def issue(self, organization_id: UUID) -> tuple[str, str]: ...

    def parse_and_hash(self, cookie_value: str) -> tuple[UUID, str] | None: ...


class AuthRepository(Protocol):
    """Persistence operations available inside one authentication transaction."""

    async def find_organization(self, slug: str) -> tuple[UUID, str] | None: ...

    async def find_user(self, normalized_email: str) -> LoginIdentity | None: ...

    async def activate_tenant(self, organization_id: UUID) -> None: ...

    async def find_membership_actor(
        self, organization_id: UUID, identity: LoginIdentity
    ) -> AuthenticatedActor | None: ...

    async def find_current_actor_by_membership(
        self, *, organization_id: UUID, membership_id: UUID
    ) -> AuthenticatedActor | None: ...

    async def create_session(
        self,
        *,
        organization_id: UUID,
        membership_id: UUID,
        token_hash: str,
        expires_at: datetime,
    ) -> None: ...

    async def find_active_session_actor(
        self, *, organization_id: UUID, token_hash: str, now: datetime
    ) -> AuthenticatedActor | None: ...

    async def revoke_session(
        self, *, organization_id: UUID, token_hash: str, now: datetime
    ) -> AuthenticatedActor | None: ...

    async def add_audit_event(
        self,
        *,
        organization_id: UUID,
        actor_membership_id: UUID | None,
        action: str,
        outcome: str,
        request_id: str,
        reason_data: dict[str, object] | None = None,
    ) -> None: ...


AuthTransactionFactory = Callable[[], AbstractAsyncContextManager[AuthRepository]]
