# pyright: reportPrivateUsage=false

import pytest
from pydantic import TypeAdapter, ValidationError

from app.modules.assistant.api.schemas import ContentBlock
from tests.retrieval_public_fixtures import public_answer

_ADAPTER: TypeAdapter[ContentBlock] = TypeAdapter(ContentBlock)


def test_public_retrieval_contract_preserves_unknown_dates():
    value = _ADAPTER.validate_python(public_answer()).model_dump(mode="json")
    assert value["sources"][0]["published_at"] is None
    assert value["sources"][0]["source_fetched_at"] is None
    assert value["sources"][0]["method"] == "SEARCH_SNIPPET"
    assert "usage" not in value and "provider_version" not in value["sources"][0]


@pytest.mark.parametrize(
    "mutation",
    ["unknown-citation", "unsafe-url", "provider-error", "auth-metadata", "unsupported-version"],
)
def test_public_retrieval_rejects_invalid_or_internal_payload(mutation: str):
    value = public_answer()
    if mutation == "unknown-citation":
        value["claims"][0]["evidence_ids"] = ["33333333-3333-4333-8333-333333333333"]
    elif mutation == "unsafe-url":
        value["sources"][0]["canonical_url"] = "javascript:alert(1)"
    elif mutation == "provider-error":
        value["raw_provider_error"] = "secret"
    elif mutation == "auth-metadata":
        value["sources"][0]["organization_id"] = value["context_run_id"]
    else:
        value["schema_version"] = "99.0"
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python(value)


def test_unavailable_public_block_carries_no_claims_or_sources():
    value = public_answer()
    value.update(availability="EXPIRED", claims=[], sources=[], limitations=[])
    result = _ADAPTER.validate_python(value).model_dump(mode="json")
    assert result["availability"] == "EXPIRED"
    assert not result["sources"]


@pytest.mark.asyncio
async def test_storage_preparation_preserves_existing_planning_authorization():
    from typing import Any, cast
    from uuid import uuid4

    from app.modules.assistant.adapters.planning_projection import PlanningBlockProjector
    from app.modules.assistant.adapters.retrieval_projection import RetrievalBlockProjector
    from app.modules.planning_runs.application.draft_service import PlanningDraftService
    from tests.test_assistant_sse import _actor

    class UnavailableDrafts:
        async def get_snapshot(self, **kwargs: Any) -> Any:
            raise PermissionError("no longer permitted")

    previous = PlanningBlockProjector(
        drafts=cast(PlanningDraftService, UnavailableDrafts()), transactions=None
    )
    projector = RetrievalBlockProjector(budgets=cast(Any, None), previous=previous)
    block = dict(
        kind="planning_draft",
        workflow_run_id=str(uuid4()),
        draft_id=str(uuid4()),
        draft_version=1,
        summary="Private old draft",
        state="DRAFT_AVAILABLE",
        conflicts=[],
    )
    stored = await projector.prepare_for_storage(_actor(), [block])
    assert stored[0]["kind"] == "safe_error"
    assert "Private old draft" not in str(stored)
