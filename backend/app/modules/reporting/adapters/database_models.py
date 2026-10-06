"""Tenant-qualified report containers and append-only snapshot/version inventories."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ReportModel(Base):
    __tablename__ = "reports"
    __table_args__ = (
        UniqueConstraint("organization_id", "project_id", "id"),
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "project_id"], ["projects.organization_id", "projects.id"]
        ),
        ForeignKeyConstraint(
            ["organization_id", "created_by_membership_id"],
            ["memberships.organization_id", "memberships.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "id", "snapshot_id"],
            [
                "report_metric_snapshots.organization_id",
                "report_metric_snapshots.report_id",
                "report_metric_snapshots.id",
            ],
            name="fk_reports_owned_snapshot",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["organization_id", "id", "selected_version_id"],
            ["report_versions.organization_id", "report_versions.report_id", "report_versions.id"],
            name="fk_reports_owned_version",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["organization_id", "id", "current_publication_id"],
            [
                "report_publications.organization_id",
                "report_publications.report_id",
                "report_publications.id",
            ],
            name="fk_reports_owned_publication",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        CheckConstraint("kind IN ('DAILY','WEEKLY')", name="kind"),
        CheckConstraint("locale IN ('vi','en')", name="locale"),
        CheckConstraint("version >= 1", name="version"),
        Index("ix_reports_project_timeline", "organization_id", "project_id", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    project_id: Mapped[UUID]
    kind: Mapped[str] = mapped_column(String(16))
    locale: Mapped[str] = mapped_column(String(2))
    version: Mapped[int] = mapped_column(Integer)
    snapshot_id: Mapped[UUID]
    selected_version_id: Mapped[UUID]
    current_publication_id: Mapped[UUID | None]
    created_by_membership_id: Mapped[UUID]
    narrative_requested: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReportSnapshotModel(Base):
    __tablename__ = "report_metric_snapshots"
    __table_args__ = (
        UniqueConstraint("organization_id", "report_id", "id", "snapshot_hash"),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "report_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "report_id"],
            ["reports.organization_id", "reports.id"],
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["organization_id", "project_id"], ["projects.organization_id", "projects.id"]
        ),
        CheckConstraint("length(snapshot_hash)=64", name="hash"),
        Index("ix_report_snapshots_project", "organization_id", "project_id", "captured_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    report_id: Mapped[UUID]
    project_id: Mapped[UUID]
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReportVersionModel(Base):
    __tablename__ = "report_versions"
    __table_args__ = (
        UniqueConstraint("organization_id", "report_id", "id", "snapshot_id"),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "report_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "report_id", "snapshot_id"],
            [
                "report_metric_snapshots.organization_id",
                "report_metric_snapshots.report_id",
                "report_metric_snapshots.id",
            ],
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("origin IN ('METRICS_ONLY','AI_PROPOSED','AI_EDITED')", name="origin"),
        CheckConstraint("locale IN ('vi','en')", name="locale"),
        Index("ix_report_versions_timeline", "organization_id", "report_id", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    report_id: Mapped[UUID]
    snapshot_id: Mapped[UUID]
    origin: Mapped[str] = mapped_column(String(24))
    locale: Mapped[str] = mapped_column(String(2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")


class ReportSourceModel(Base):
    __tablename__ = "report_snapshot_sources"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "snapshot_id", "task_id"),
        ForeignKeyConstraint(
            ["organization_id", "snapshot_id"],
            ["report_metric_snapshots.organization_id", "report_metric_snapshots.id"],
        ),
        ForeignKeyConstraint(["organization_id", "task_id"], ["tasks.organization_id", "tasks.id"]),
        Index("ix_report_sources_snapshot", "organization_id", "snapshot_id", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    snapshot_id: Mapped[UUID]
    task_id: Mapped[UUID]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ReportReceiptModel(Base):
    __tablename__ = "report_snapshot_receipts"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "snapshot_id"],
            ["report_metric_snapshots.organization_id", "report_metric_snapshots.id"],
        ),
        Index("ix_report_receipts_snapshot", "organization_id", "snapshot_id", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    snapshot_id: Mapped[UUID]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ReportReviewDecisionModel(Base):
    __tablename__ = "report_review_decisions"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint(
            "organization_id",
            "report_id",
            "report_version_id",
            "snapshot_hash",
            "actor_membership_id",
            "id",
        ),
        ForeignKeyConstraint(
            ["organization_id", "report_id", "report_version_id"],
            ["report_versions.organization_id", "report_versions.report_id", "report_versions.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "actor_membership_id"],
            ["memberships.organization_id", "memberships.id"],
        ),
        CheckConstraint("kind IN ('METRICS_ONLY_PUBLISHED','ACCEPT','REJECT')", name="kind"),
        CheckConstraint("length(snapshot_hash)=64", name="hash"),
        CheckConstraint("expected_report_version>=1", name="expected_version"),
        Index("ix_report_decisions_timeline", "organization_id", "report_id", "decided_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    report_id: Mapped[UUID]
    report_version_id: Mapped[UUID]
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    actor_membership_id: Mapped[UUID]
    expected_report_version: Mapped[int]
    kind: Mapped[str] = mapped_column(String(32))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReportPublicationModel(Base):
    __tablename__ = "report_publications"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "report_id", "id"),
        UniqueConstraint("organization_id", "decision_id"),
        ForeignKeyConstraint(
            [
                "organization_id",
                "report_id",
                "report_version_id",
                "snapshot_hash",
                "publisher_membership_id",
                "decision_id",
            ],
            [
                "report_review_decisions.organization_id",
                "report_review_decisions.report_id",
                "report_review_decisions.report_version_id",
                "report_review_decisions.snapshot_hash",
                "report_review_decisions.actor_membership_id",
                "report_review_decisions.id",
            ],
        ),
        CheckConstraint("length(snapshot_hash)=64", name="hash"),
        Index(
            "ix_report_publications_timeline", "organization_id", "report_id", "published_at", "id"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    report_id: Mapped[UUID]
    report_version_id: Mapped[UUID]
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    publisher_membership_id: Mapped[UUID]
    decision_id: Mapped[UUID]
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
