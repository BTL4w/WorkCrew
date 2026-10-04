"""Reporting tools call authorized application services, never SQL directly."""

from typing import Protocol
from uuid import UUID

from work_management_ai.agents.reporting.contracts import (
    Contract,
    ReportingContext,
    ReportingProposal,
    ReportingRequest,
)
from work_management_ai.runtime.contracts import ActorReference


class ProposalReceipt(Contract):
    version_id: UUID


class ReportDraftPort(Protocol):
    async def read(
        self, *, actor: ActorReference, request: ReportingRequest
    ) -> ReportingContext: ...
    async def propose(
        self, *, actor: ActorReference, proposal: ReportingProposal, idempotency_key: str
    ) -> ProposalReceipt: ...
