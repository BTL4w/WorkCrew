"""PostgreSQL reservations, tenant guards and retention locks."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.audit.adapters.database_models import AuditEventModel
from app.modules.audit.domain.events import AuditOutcome
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.adapters.database_models import MembershipModel
from app.modules.planning_runs.adapters.database_models import OutboxEventModel
from app.modules.progress.adapters.evidence_models import EvidenceOriginalModel
from app.modules.progress.application.evidence_ports import EvidenceRepository
from app.modules.progress.domain.evidence import (
    EvidenceError,
    EvidenceOriginal,
    EvidenceVersionRef,
    StoredBlob,
    UploadReservation,
)


class SqlAlchemyEvidenceRepository:
    def __init__(self, session: AsyncSession, actor: AuthenticatedActor) -> None:
        self.session = session
        self.actor = actor

    async def _active(self) -> None:
        active = await self.session.scalar(
            text("SELECT public.lock_active_membership(:organization_id, :membership_id)"),
            {
                "organization_id": self.actor.organization_id,
                "membership_id": self.actor.membership_id,
            },
        )
        user = await self.session.scalar(
            select(MembershipModel.user_id).where(
                MembershipModel.organization_id == self.actor.organization_id,
                MembershipModel.id == self.actor.membership_id,
            )
        )
        if active is not True or user != self.actor.user_id:
            raise EvidenceError("FORBIDDEN", 403)

    async def reserve(self, filename: str, idempotency_key: str) -> UploadReservation:
        await self._active()
        lock = f"{self.actor.organization_id}:{self.actor.membership_id}:evidence:{idempotency_key}"
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": lock}
        )
        model = await self.session.scalar(
            select(EvidenceOriginalModel)
            .where(
                EvidenceOriginalModel.organization_id == self.actor.organization_id,
                EvidenceOriginalModel.uploader_membership_id == self.actor.membership_id,
                EvidenceOriginalModel.idempotency_key == idempotency_key,
            )
            .with_for_update()
        )
        now = datetime.now(UTC)
        if model is None:
            evidence_id = uuid4()
            model = EvidenceOriginalModel(
                id=evidence_id,
                organization_id=self.actor.organization_id,
                uploader_membership_id=self.actor.membership_id,
                version=1,
                filename=filename,
                idempotency_key=idempotency_key,
                storage_key=f"{self.actor.organization_id}/{evidence_id}/1",
                state="RESERVED",
                uploaded_at=now,
                expires_at=now + timedelta(days=7),
                lease_id=uuid4(),
                lease_until=now,
            )
            self.session.add(model)
        elif model.filename != filename:
            raise EvidenceError("IDEMPOTENCY_KEY_REUSED", 409)
        elif model.state == "EXPIRED" or (model.expires_at <= now and model.confirmed_at is None):
            raise EvidenceError("EVIDENCE_EXPIRED", 410)
        elif model.lease_until > now:
            raise EvidenceError("EVIDENCE_UPLOAD_IN_PROGRESS", 409)
        model.lease_id = uuid4()
        model.lease_until = now + timedelta(minutes=10)
        await self.session.flush()
        return UploadReservation(
            EvidenceVersionRef(model.id, model.version), model.storage_key, model.lease_id
        )

    async def _locked(self, reservation: UploadReservation) -> EvidenceOriginalModel:
        model = await self.session.scalar(
            select(EvidenceOriginalModel)
            .where(
                EvidenceOriginalModel.organization_id == self.actor.organization_id,
                EvidenceOriginalModel.id == reservation.ref.evidence_id,
                EvidenceOriginalModel.uploader_membership_id == self.actor.membership_id,
            )
            .with_for_update()
        )
        if model is None or model.lease_id != reservation.lease_id or model.state == "EXPIRED":
            raise EvidenceError("EVIDENCE_UPLOAD_IN_PROGRESS", 409)
        return model

    async def finalize(self, reservation: UploadReservation, blob: StoredBlob) -> bool:
        await self._active()
        model = await self._locked(reservation)
        replayed = model.state == "READY"
        if replayed and (model.sha256, model.byte_length, model.mime_type) != (
            blob.sha256,
            blob.byte_length,
            blob.detected_mime,
        ):
            raise EvidenceError("IDEMPOTENCY_KEY_REUSED", 409)
        if not replayed:
            model.sha256 = blob.sha256
            model.byte_length = blob.byte_length
            model.mime_type = blob.detected_mime
            model.state = "READY"
            event_id = uuid4()
            self.session.add(
                OutboxEventModel(
                    id=event_id,
                    event_id=event_id,
                    organization_id=self.actor.organization_id,
                    event_type="evidence.uploaded",
                    aggregate_type="evidence",
                    aggregate_id=model.id,
                    envelope_version="1.0",
                    payload={
                        "evidence_id": str(model.id),
                        "version": model.version,
                        "sha256": blob.sha256,
                    },
                )
            )
        model.lease_until = datetime.now(UTC)
        return replayed

    async def get(self, ref: EvidenceVersionRef) -> EvidenceOriginal:
        await self._active()
        model = await self.session.scalar(
            select(EvidenceOriginalModel)
            .where(
                EvidenceOriginalModel.organization_id == self.actor.organization_id,
                EvidenceOriginalModel.id == ref.evidence_id,
                EvidenceOriginalModel.version == ref.version,
                EvidenceOriginalModel.uploader_membership_id == self.actor.membership_id,
                EvidenceOriginalModel.state == "READY",
            )
            .with_for_update(read=True)
        )
        if model is None or (model.confirmed_at is None and model.expires_at <= datetime.now(UTC)):
            raise EvidenceError("RESOURCE_NOT_FOUND", 404)
        assert model.sha256 is not None and model.byte_length is not None and model.mime_type
        return EvidenceOriginal(
            ref,
            model.filename,
            StoredBlob(model.storage_key, model.sha256, model.byte_length, model.mime_type),
            model.uploaded_at,
            None if model.confirmed_at else model.expires_at,
        )

    async def release(self, reservation: UploadReservation) -> None:
        model = await self._locked(reservation)
        model.lease_until = datetime.now(UTC)

    async def audit(
        self,
        action: str,
        request_id: str,
        code: str | None = None,
        resource_id: UUID | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        self.session.add(
            AuditEventModel(
                id=uuid4(),
                organization_id=self.actor.organization_id,
                actor_membership_id=self.actor.membership_id,
                action=action,
                outcome=AuditOutcome.REJECTED if code else AuditOutcome.SUCCEEDED,
                resource_type="evidence",
                resource_id=resource_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                before_data={},
                after_data={},
                reason_data={"code": code} if code else {},
            )
        )

    async def expired(self, at: datetime) -> tuple[UploadReservation, ...]:
        models = await self.session.scalars(
            select(EvidenceOriginalModel)
            .where(
                EvidenceOriginalModel.organization_id == self.actor.organization_id,
                EvidenceOriginalModel.uploader_membership_id == self.actor.membership_id,
                EvidenceOriginalModel.confirmed_at.is_(None),
                EvidenceOriginalModel.expires_at <= at,
                EvidenceOriginalModel.lease_until <= at,
                EvidenceOriginalModel.state != "EXPIRED",
            )
            .limit(100)
            .with_for_update(skip_locked=True)
        )
        return tuple(
            UploadReservation(EvidenceVersionRef(m.id, m.version), m.storage_key, m.lease_id)
            for m in models
        )

    async def expire(self, reservation: UploadReservation) -> None:
        model = await self._locked(reservation)
        if model.confirmed_at is not None or model.lease_until > datetime.now(UTC):
            raise EvidenceError("EVIDENCE_RETENTION_CONFLICT", 409)
        model.state = "EXPIRED"


class SqlAlchemyEvidenceTransactionFactory:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    @asynccontextmanager
    async def __call__(self, actor: AuthenticatedActor) -> AsyncGenerator[EvidenceRepository]:
        async with self.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE app_runtime"))
            await session.execute(
                text("SELECT set_config('app.organization_id', :value, true)"),
                {"value": str(actor.organization_id)},
            )
            await session.execute(
                text("SELECT set_config('app.membership_id', :value, true)"),
                {"value": str(actor.membership_id)},
            )
            yield SqlAlchemyEvidenceRepository(session, actor)
