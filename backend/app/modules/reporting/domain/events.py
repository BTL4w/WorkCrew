"""Versioned fact envelope; capturing metrics alone requests no worker behavior."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from .metrics import ReportContract


class MetricsCaptured(ReportContract):
    schema_version: Literal["1.0"] = "1.0"
    report_id: UUID
    snapshot_id: UUID
    snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor_membership_id: UUID


class ReportPublished(ReportContract):
    schema_version: Literal["1.0"] = "1.0"
    report_id: UUID
    publication_id: UUID
    report_version_id: UUID
    snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor_membership_id: UUID
