"""Evidence HTTP contract, real PostgreSQL authorization and upload replay."""

import asyncio

# All PostgreSQL tests use an outer rollback transaction and real app_runtime RLS.
import io
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.database import create_database_engine
from app.main import create_app
from app.modules.identity.api.dependencies import get_authenticated_actor
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.progress.adapters.evidence_repository import SqlAlchemyEvidenceTransactionFactory
from app.modules.progress.adapters.filesystem_storage import FilesystemEvidenceStorage
from app.modules.progress.application.evidence_ports import EvidenceRepository
from app.modules.progress.application.evidence_service import EvidenceService
from app.modules.progress.domain.evidence import EvidenceVersionRef


@pytest.mark.asyncio
async def test_evidence_upload_requires_authentication() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=create_app(Settings(environment="test"))),
        base_url="http://test",
    ) as client:
        response = await client.post("/api/v1/evidence", content=b"file")
    assert response.status_code == 401


postgres = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires local PostgreSQL"
)


@dataclass
class Harness:
    actor: AuthenticatedActor
    peer: AuthenticatedActor
    foreign: AuthenticatedActor
    service: EvidenceService
    connection: AsyncConnection
    transactions: SqlAlchemyEvidenceTransactionFactory
    root: Path

    async def sql(
        self, statement: str, parameters: dict[str, object] | None = None
    ) -> CursorResult[Any]:
        await self.connection.execute(text("RESET ROLE"))
        return await self.connection.execute(text(statement), parameters or {})

    def app(self, actor: AuthenticatedActor | None = None) -> FastAPI:
        app = create_app(Settings(environment="test"), evidence_service=self.service)
        app.dependency_overrides[get_authenticated_actor] = lambda: actor or self.actor
        return app


@pytest_asyncio.fixture
async def harness(tmp_path: Path) -> AsyncGenerator[Harness]:
    engine = create_database_engine(Settings(environment="test"))
    async with engine.connect() as connection:
        transaction = await connection.begin()
        actors: list[AuthenticatedActor] = []
        first_org = uuid4()
        for index in range(3):
            org = first_org if index < 2 else uuid4()
            if index != 1:
                await connection.execute(
                    text("INSERT INTO organizations (id, slug, name) VALUES (:id, :slug, :name)"),
                    {"id": org, "slug": f"evidence-{org}", "name": "Evidence Tenant"},
                )
            user, member = uuid4(), uuid4()
            email = f"{user}@example.test"
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, email_normalized, email_display, display_name, password_hash) "
                    "VALUES (:id, :email, :email, 'Uploader', 'unused')"
                ),
                {"id": user, "email": email},
            )
            await connection.execute(
                text(
                    "INSERT INTO memberships "
                    "(id, organization_id, user_id, role) VALUES (:id, :org, :user, :role)"
                ),
                {
                    "id": member,
                    "org": org,
                    "user": user,
                    "role": "EMPLOYEE" if index == 0 else "MANAGER",
                },
            )
            actors.append(
                AuthenticatedActor(
                    user,
                    email,
                    "Uploader",
                    member,
                    org,
                    "Tenant",
                    MembershipRole.EMPLOYEE if index == 0 else MembershipRole.MANAGER,
                )
            )
        sessions = async_sessionmaker(
            bind=connection,
            class_=AsyncSession,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        transactions = SqlAlchemyEvidenceTransactionFactory(sessions)
        root = tmp_path / "private"
        service = EvidenceService(transactions, FilesystemEvidenceStorage(root))
        yield Harness(actors[0], actors[1], actors[2], service, connection, transactions, root)
        await transaction.rollback()
    await engine.dispose()


def image_bytes(color: str = "white") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(buffer, format="PNG")
    return buffer.getvalue()


async def upload(client: AsyncClient, body: bytes | None = None, key: str = "evidence-upload-01"):
    return await client.post(
        "/api/v1/evidence",
        content=body or image_bytes(),
        headers={
            "Content-Type": "image/png",
            "X-Evidence-Filename": "proof.png",
            "Idempotency-Key": key,
        },
    )


@pytest.mark.integration
@postgres
@pytest.mark.asyncio
async def test_upload_replay_preserves_single_version(harness: Harness) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=harness.app()), base_url="http://test"
    ) as client:
        first = await upload(client)
        assert first.status_code == 201, first.text
        replay = await upload(client)
        assert replay.status_code == 201, replay.text
        first_ref, replay_ref = first.json(), replay.json()
        assert first_ref == replay_ref
        changed = await upload(client, image_bytes("black"))
        assert changed.status_code == 409
        content_path = f"/api/v1/evidence/{first_ref['evidence_id']}/versions/1/content"
        downloaded = await client.get(content_path)
        assert downloaded.content == image_bytes()
        assert downloaded.headers["cache-control"] == "private, no-store"
        assert downloaded.headers["x-content-type-options"] == "nosniff"
        assert "attachment;" in downloaded.headers["content-disposition"]
        assert "storage_key" not in first_ref
    count = await harness.sql(
        "SELECT count(*) FROM evidence_originals WHERE organization_id = :org",
        {"org": harness.actor.organization_id},
    )
    stored_version_count = count.scalar_one()
    assert stored_version_count == 1
    async with AsyncClient(
        transport=ASGITransport(app=harness.app(harness.foreign)), base_url="http://test"
    ) as client:
        foreign_tenant_response = await client.get(content_path)
    assert foreign_tenant_response.status_code == 404
    audits = await harness.sql(
        "SELECT action FROM audit_events WHERE resource_type = :kind", {"kind": "evidence"}
    )
    actions = list(audits.scalars())
    assert "evidence.uploaded" in actions
    assert "evidence.upload.rejected" in actions
    assert "evidence.downloaded" in actions
    assert "evidence.download.rejected" in actions


@pytest.mark.integration
@postgres
@pytest.mark.asyncio
async def test_staged_owner_only_and_download_rechecks_membership(harness: Harness) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=harness.app()), base_url="http://test"
    ) as client:
        original = (await upload(client)).json()
        path = f"/api/v1/evidence/{original['evidence_id']}/versions/1/content"
        assert (await client.get(path)).status_code == 200
        async with AsyncClient(
            transport=ASGITransport(app=harness.app(harness.peer)), base_url="http://test"
        ) as peer:
            assert (await peer.get(path)).status_code == 404
        await harness.sql(
            "UPDATE memberships SET is_active=false WHERE id=:id",
            {"id": harness.actor.membership_id},
        )
        assert (await client.get(path)).status_code == 403
        assert (await upload(client, key="after-membership-revocation")).status_code == 403


@pytest.mark.integration
@postgres
@pytest.mark.asyncio
async def test_spoofed_mime_invalid_headers_and_oversize_are_audited(harness: Harness) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=harness.app()), base_url="http://test"
    ) as client:
        for headers, body, expected in [
            (
                {
                    "Content-Type": "application/pdf",
                    "X-Evidence-Filename": "proof.pdf",
                    "Idempotency-Key": "spoofed-evidence-01",
                },
                image_bytes(),
                422,
            ),
            ({"Content-Type": "image/png", "X-Evidence-Filename": "proof.png"}, image_bytes(), 400),
            ({"Content-Length": str(20 * 1024 * 1024 + 1)}, b"x", 413),
            (
                {
                    "Content-Type": "image/png",
                    "X-Evidence-Filename": "..%2Fproof.png",
                    "Idempotency-Key": "traversal-evidence-01",
                },
                image_bytes(),
                422,
            ),
        ]:
            response = await client.post("/api/v1/evidence", content=body, headers=headers)
            assert response.status_code == expected, response.text
            assert "error" in response.json()
    result = await harness.sql(
        "SELECT count(*) FROM audit_events WHERE "
        "action='evidence.upload.rejected' AND organization_id=:org",
        {"org": harness.actor.organization_id},
    )
    assert result.scalar_one() == 4


@pytest.mark.integration
@postgres
@pytest.mark.asyncio
async def test_rls_defaults_to_deny_and_checks_owner_and_tenant(harness: Harness) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=harness.app()), base_url="http://test"
    ) as client:
        assert (await upload(client)).status_code == 201
    await harness.sql("SET LOCAL ROLE app_runtime")
    await harness.connection.execute(
        text(
            "SELECT set_config('app.organization_id','',true), "
            "set_config('app.membership_id','',true)"
        )
    )
    assert (
        await harness.connection.execute(text("SELECT count(*) FROM evidence_originals"))
    ).scalar() == 0
    await harness.connection.execute(
        text(
            "SELECT set_config('app.organization_id',:org,true), "
            "set_config('app.membership_id',:member,true)"
        ),
        {"org": str(harness.peer.organization_id), "member": str(harness.peer.membership_id)},
    )
    assert (
        await harness.connection.execute(text("SELECT count(*) FROM evidence_originals"))
    ).scalar() == 0
    role = await harness.sql("SELECT rolbypassrls FROM pg_roles WHERE rolname='app_runtime'")
    assert role.scalar_one() is False


class OldClock(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        return datetime.now(UTC) - timedelta(days=8)


@pytest.mark.integration
@postgres
@pytest.mark.asyncio
async def test_retention_expiry_protects_confirmed_and_active_uploads(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.progress.adapters import evidence_repository

    with monkeypatch.context() as patch:
        patch.setattr(evidence_repository, "datetime", OldClock)
        async with AsyncClient(
            transport=ASGITransport(app=harness.app()), base_url="http://test"
        ) as client:
            staged = (await upload(client, key="expired-staged-upload")).json()
            confirmed = (await upload(client, key="confirmed-upload-01")).json()
            active = (await upload(client, key="active-upload-lease")).json()
    await harness.sql(
        "UPDATE evidence_originals SET confirmed_at=now() WHERE id=:id",
        {"id": UUID(confirmed["evidence_id"])},
    )
    await harness.sql(
        "UPDATE evidence_originals SET lease_until=now()+interval '10 minutes' WHERE id=:id",
        {"id": UUID(active["evidence_id"])},
    )
    assert await harness.service.cleanup_expired(harness.actor) == 1
    assert await harness.service.cleanup_expired(harness.actor) == 0
    assert not (
        harness.root / str(harness.actor.organization_id) / staged["evidence_id"] / "1"
    ).exists()
    confirmed_original = await harness.service.get(
        harness.actor, EvidenceVersionRef(UUID(confirmed["evidence_id"]), 1)
    )
    assert confirmed_original.expires_at is None
    assert (harness.root / confirmed_original.blob.key).exists()
    assert (
        harness.root / str(harness.actor.organization_id) / active["evidence_id"] / "1"
    ).exists()


@pytest.mark.integration
@postgres
@pytest.mark.asyncio
async def test_failed_metadata_commit_recovers_original_after_lease(harness: Harness) -> None:
    class FailedFinalize:
        def __init__(self, repository: EvidenceRepository) -> None:
            self.repository = repository

        def __getattr__(self, name: str):
            return getattr(self.repository, name)

        async def finalize(self, *args: object) -> bool:
            raise RuntimeError("database interrupted")

    @asynccontextmanager
    async def failing(actor: AuthenticatedActor) -> AsyncGenerator[EvidenceRepository]:
        async with harness.transactions(actor) as repository:
            yield FailedFinalize(repository)  # type: ignore[misc]

    harness.service.transactions = failing
    async with AsyncClient(
        transport=ASGITransport(app=harness.app(), raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        failed = await upload(client)
        assert failed.status_code == 500
    harness.service.transactions = harness.transactions
    await harness.sql("UPDATE evidence_originals SET lease_until=now()-interval '1 second'")
    async with AsyncClient(
        transport=ASGITransport(app=harness.app()), base_url="http://test"
    ) as client:
        recovered = await upload(client)
        assert recovered.status_code == 201, recovered.text
        assert (await upload(client)).json() == recovered.json()
    assert len(await asyncio.to_thread(lambda: list(harness.root.rglob("1")))) == 1


@pytest.mark.integration
@postgres
@pytest.mark.asyncio
async def test_active_reservation_denies_competing_upload(harness: Harness) -> None:
    async with harness.transactions(harness.actor) as repository:
        await repository.reserve("proof.png", "competing-upload-01")
    async with AsyncClient(
        transport=ASGITransport(app=harness.app()), base_url="http://test"
    ) as client:
        response = await upload(client, key="competing-upload-01")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EVIDENCE_UPLOAD_IN_PROGRESS"


@pytest.mark.integration
@postgres
@pytest.mark.asyncio
async def test_download_revoked_during_first_read_denies_and_closes_stream(
    harness: Harness,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=harness.app()), base_url="http://test"
    ) as client:
        original = (await upload(client)).json()
    storage = FilesystemEvidenceStorage(harness.root)
    closed: list[bool] = []

    class RevokingStorage(FilesystemEvidenceStorage):
        async def open(self, key: str) -> AsyncGenerator[bytes]:
            try:
                await harness.sql(
                    "UPDATE memberships SET is_active=false WHERE id=:id",
                    {"id": harness.actor.membership_id},
                )
                async for part in storage.open(key):
                    yield part
            finally:
                closed.append(True)

    harness.service.storage = RevokingStorage(harness.root)
    async with AsyncClient(
        transport=ASGITransport(app=harness.app()), base_url="http://test"
    ) as client:
        path = f"/api/v1/evidence/{original['evidence_id']}/versions/1/content"
        response = await client.get(path)
    assert response.status_code == 403
    assert closed == [True]


@pytest.mark.integration
@postgres
@pytest.mark.asyncio
async def test_original_metadata_is_immutable_and_uploader_fk_cannot_cross_tenants(
    harness: Harness,
) -> None:
    from sqlalchemy.exc import DBAPIError

    async with AsyncClient(
        transport=ASGITransport(app=harness.app()), base_url="http://test"
    ) as client:
        created = (await upload(client)).json()
    await harness.sql("RESET ROLE")
    async with harness.connection.begin_nested() as savepoint:
        with pytest.raises(DBAPIError, match="immutable evidence metadata"):
            await harness.connection.execute(
                text("UPDATE evidence_originals SET filename=:filename WHERE id=:id"),
                {"filename": "replacement.png", "id": UUID(created["evidence_id"])},
            )
        await savepoint.rollback()
    async with harness.connection.begin_nested() as savepoint:
        with pytest.raises(DBAPIError) as error:
            await harness.connection.execute(
                text(
                    "INSERT INTO evidence_originals "
                    "(id, organization_id, uploader_membership_id, version, filename, "
                    "idempotency_key, "
                    "storage_key, state, uploaded_at, expires_at, lease_id, lease_until) "
                    "VALUES (:id,:org,:foreign_member,1,'proof.png','cross-tenant-upload',:key,"
                    "'RESERVED',now(),now()+interval '7 days',:lease,now())"
                ),
                {
                    "id": uuid4(),
                    "org": harness.actor.organization_id,
                    "foreign_member": harness.foreign.membership_id,
                    "key": str(uuid4()),
                    "lease": uuid4(),
                },
            )
        assert "foreign key constraint" in str(error.value)
        await savepoint.rollback()
    result = await harness.sql(
        "SELECT count(*) FROM outbox_events WHERE organization_id=:org "
        "AND event_type='evidence.uploaded'",
        {"org": harness.actor.organization_id},
    )
    assert result.scalar_one() == 1
