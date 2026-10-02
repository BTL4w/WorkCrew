from collections.abc import AsyncIterator
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.progress.adapters.daily_update_comparison import OriginalEvidenceComparison
from app.modules.progress.application.assessment_service import ComparisonBudget, OriginalSource
from app.modules.progress.domain.daily_updates import SelectedEvidence
from app.modules.progress.domain.evidence import (
    AuthorizedEvidenceStream,
    EvidenceOriginal,
    EvidenceVersionRef,
    StoredBlob,
)
from app.modules.progress.domain.evidence_support import Claim
from work_management_ai.model_gateway.mock import MockModelGateway
from work_management_ai.runtime.budgeted_gateway import BudgetedDailyGateway
from work_management_ai.runtime.daily_update_budget import (
    BudgetScope,
    MemoryUsageStore,
    daily_model_scope,
)


class Originals:
    def __init__(self, data: bytes):
        self.data = data
        self.opened = 0

    async def open_version(
        self, actor: AuthenticatedActor, ref: EvidenceVersionRef, request_id: str
    ) -> AuthorizedEvidenceStream:
        self.opened += 1

        async def content() -> AsyncIterator[bytes]:
            yield self.data

        return AuthorizedEvidenceStream(
            EvidenceOriginal(
                ref,
                "proof.png",
                StoredBlob("key", sha256(self.data).hexdigest(), len(self.data), "image/png"),
                datetime.now(UTC),
                None,
            ),
            content(),
        )


@pytest.mark.asyncio
async def test_original_bytes_are_passed_directly_and_receipts_are_server_owned():
    org, member, evidence = uuid4(), uuid4(), uuid4()
    data = b"\x89PNG\r\n\x1a\noriginal"
    ref = SelectedEvidence(evidence_id=evidence, version=1)
    claim = Claim(
        id="0:0",
        source_span="items[0].done_text:line:0",
        text="Completed survey",
        task_id=uuid4(),
        evidence_refs=(ref,),
    )
    fixtures = {
        "daily_update.claims": {
            "claims": [
                {
                    "source_claim_id": "0:0",
                    "text": "Completed survey",
                    "category": "WORK",
                    "checkability": True,
                }
            ]
        },
        "daily_update.compare": {
            "findings": [
                {
                    "claim_id": "c0",
                    "finding": "SUPPORTED",
                    "source_refs": [ref.model_dump(mode="json")],
                    "limitation": "",
                }
            ]
        },
    }
    original = Originals(data)
    gateway = BudgetedDailyGateway(
        MockModelGateway(fixtures=fixtures), MemoryUsageStore(), image_token_bound=6000
    )
    adapter = OriginalEvidenceComparison(
        gateway=gateway,
        evidence=original,
        actor=AuthenticatedActor(
            user_id=uuid4(),
            email="test@example.com",
            display_name="Test",
            membership_id=member,
            organization_id=org,
            organization_name="Test",
            role=MembershipRole.EMPLOYEE,
        ),
    )
    with daily_model_scope(BudgetScope(organization_id=org, membership_id=member, run_id=uuid4())):
        result = await adapter.compare(
            (claim,),
            (
                OriginalSource(
                    evidence_id=evidence,
                    version=1,
                    sha256=sha256(data).hexdigest(),
                    mime_type="image/png",
                ),
            ),
            ComparisonBudget(),
        )
    assert result.processed_sources == (ref,)
    assert result.claims is not None and result.claims[0].text == claim.text
    assert original.opened == 2


@pytest.mark.asyncio
async def test_unsupported_original_does_not_get_silently_dropped():
    source = OriginalSource(
        evidence_id=uuid4(), version=1, sha256="a" * 64, mime_type="application/pdf"
    )
    original = Originals(b"%PDF-1.7")
    adapter = OriginalEvidenceComparison(
        gateway=MockModelGateway(fixtures={}),
        evidence=original,
        actor=AuthenticatedActor(
            user_id=uuid4(),
            email="test@example.com",
            display_name="Test",
            membership_id=uuid4(),
            organization_id=uuid4(),
            organization_name="Test",
            role=MembershipRole.EMPLOYEE,
        ),
    )
    with pytest.raises(ValueError, match="ORIGINAL_FORMAT_UNSUPPORTED"):
        await adapter.compare((), (source,), ComparisonBudget())
    assert original.opened == 0


@pytest.mark.asyncio
async def test_compound_report_cannot_drop_unverified_suffix():
    actor = AuthenticatedActor(
        user_id=uuid4(),
        email="test@example.com",
        display_name="Test",
        membership_id=uuid4(),
        organization_id=uuid4(),
        organization_name="Test",
        role=MembershipRole.EMPLOYEE,
    )
    claim = Claim(
        id="0:0",
        source_span="items[0].done_text:line:0",
        text="Completed survey; completed all acceptance criteria",
        task_id=uuid4(),
    )
    adapter = OriginalEvidenceComparison(
        gateway=MockModelGateway(
            fixtures={
                "daily_update.claims": {
                    "claims": [
                        {
                            "source_claim_id": "0:0",
                            "text": "Completed survey",
                            "category": "WORK",
                            "checkability": True,
                        }
                    ]
                }
            }
        ),
        evidence=Originals(b""),
        actor=actor,
        run_id=uuid4(),
    )
    with pytest.raises(ValueError, match="INCOMPLETE_CLAIM_INVENTORY"):
        await adapter.compare((claim,), (), ComparisonBudget())
