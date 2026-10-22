"""Existing evidence blocks are projected through current source access on every read."""

from typing import Any, Literal, cast
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.retrieval.application.budget_service import RetrievalBudgetService
from app.modules.retrieval.domain.state import RetrievalSnapshot
from work_management_ai.agents.retrieve.contracts import SourceBinding
from work_management_ai.runtime.contracts import (
    RetrievalAnswerResponseBlock,
    RetrievalClaim,
    RetrievalSource,
)


def _source(snapshot: RetrievalSnapshot) -> RetrievalSource:
    source = snapshot.source
    return RetrievalSource(
        evidence_id=snapshot.id,
        title=source.title,
        canonical_url=source.url.value,
        method=source.method,
        excerpt=source.text,
        published_at=source.published_at,
        source_fetched_at=source.source_fetched_at,
    )


class StoredRetrievalAnswer(BaseModel):
    """Private operational DTO. Excerpts, titles and URLs are never copied here."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["retrieval_answer"] = "retrieval_answer"
    schema_version: Literal["1.0"] = "1.0"
    context_run_id: UUID
    outcome: str
    claims: tuple[RetrievalClaim, ...] = Field(default=(), max_length=12)
    source_refs: tuple[SourceBinding, ...] = Field(default=(), max_length=8)
    limitations: tuple[str, ...] = Field(default=(), max_length=16)
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_refs(self) -> "StoredRetrievalAnswer":
        ids = {ref.evidence_id for ref in self.source_refs}
        if len(ids) != len(self.source_refs) or any(
            not set(claim.evidence_ids).issubset(ids) for claim in self.claims
        ):
            raise ValueError("RETRIEVAL_CITATION_INVALID")
        return self


class RetrievalBlockProjector:
    def __init__(self, budgets: RetrievalBudgetService, previous: Any = None):
        self.budgets, self.previous = budgets, previous

    async def prepare_for_storage(self, actor: AuthenticatedActor, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            return [await self.prepare_for_storage(actor, item) for item in cast(list[Any], value)]
        if not isinstance(value, dict):
            return value
        value = cast(dict[str, Any], value)
        if value.get("kind") == "retrieval_answer":
            block = RetrievalAnswerResponseBlock.model_validate(value)
            try:
                snapshots = await self.budgets.projection_sources(
                    actor,
                    block.context_run_id,
                    tuple(source.evidence_id for source in block.sources),
                )
            except ValueError as error:
                # bind() checked membership/tenant/owner before this operational status.
                # A failed provider result with no evidence can retain its safe outage
                # outcome; no failed-run source or claim is reused.
                if (
                    str(error) != "RETRIEVAL_RUN_STOPPED"
                    or block.outcome != "SOURCE_UNAVAILABLE"
                    or block.sources
                    or block.claims
                ):
                    raise
                snapshots = ()
            sources = tuple(_source(snapshot) for snapshot in snapshots)
            if block.sources != sources:
                raise ValueError("RETRIEVAL_SOURCE_UNAVAILABLE")
            stored = StoredRetrievalAnswer(
                context_run_id=block.context_run_id,
                outcome=block.outcome,
                claims=block.claims,
                source_refs=tuple(
                    SourceBinding(evidence_id=snapshot.id, fingerprint=snapshot.source.fingerprint)
                    for snapshot in snapshots
                ),
                limitations=block.limitations,
                observed_at=block.observed_at,
            )
            return stored.model_dump(mode="json")
        prepared = {
            key: await self.prepare_for_storage(actor, item)
            if isinstance(item, (dict, list, tuple))
            else item
            for key, item in value.items()
        }
        if value.get("kind") == "work_evidence":
            return await self.project(actor, prepared)
        if "kind" in value and self.previous is not None:
            return await self.previous.project(actor, prepared)
        return prepared

    async def _hydrate(self, actor: AuthenticatedActor, value: dict[str, Any]) -> dict[str, Any]:
        stored = StoredRetrievalAnswer.model_validate(value)
        try:
            snapshots = await self.budgets.projection_sources(
                actor,
                stored.context_run_id,
                tuple(ref.evidence_id for ref in stored.source_refs),
                expected_hashes=tuple(ref.fingerprint for ref in stored.source_refs),
            )
            sources = tuple(_source(snapshot) for snapshot in snapshots)
            return RetrievalAnswerResponseBlock.model_validate(
                {**stored.model_dump(exclude={"source_refs"}), "sources": sources}
            ).model_dump(mode="json")
        except ValueError as error:
            availability = (
                "EXPIRED"
                if str(error) in {"RETRIEVAL_CONTEXT_EXPIRED", "RETRIEVAL_SOURCE_EXPIRED"}
                else "UNAVAILABLE"
            )
            return RetrievalAnswerResponseBlock.model_validate(
                dict(
                    context_run_id=stored.context_run_id,
                    outcome=stored.outcome,
                    claims=[],
                    sources=[],
                    limitations=[],
                    observed_at=stored.observed_at,
                    availability=availability,
                )
            ).model_dump(mode="json")

    async def project(self, actor: AuthenticatedActor, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            return [await self.project(actor, item) for item in cast(list[Any], value)]
        if not isinstance(value, dict):
            return value
        value = cast(dict[str, Any], value)
        if value.get("kind") == "retrieval_answer":
            try:
                stored = (
                    value
                    if "source_refs" in value
                    else await self.prepare_for_storage(actor, value)
                )
                return await self._hydrate(actor, stored)
            except ValueError:
                return {
                    "kind": "text",
                    "text": "Web evidence unavailable / Nguồn web không còn khả dụng.",
                }
        if value.get("kind") == "work_evidence" and any(
            ref.get("resource_type") == "WEB_SOURCE_SNAPSHOT" for ref in value.get("evidence", [])
        ):
            try:
                refs = value["evidence"]
                if any(
                    ref.get("resource_type") != "WEB_SOURCE_SNAPSHOT" or ref.get("version") != 1
                    for ref in refs
                ):
                    raise ValueError("RETRIEVAL_SOURCE_UNAVAILABLE")
                await self.budgets.validate_source_ids(
                    actor, tuple(UUID(ref["resource_id"]) for ref in refs)
                )
                return value
            except ValueError:
                return {
                    "kind": "text",
                    "text": "Web evidence unavailable / Nguồn web không còn khả dụng.",
                }
        nested = {
            key: await self.project(actor, item) if isinstance(item, (dict, list, tuple)) else item
            for key, item in value.items()
        }
        return await self.previous.project(actor, nested) if self.previous else nested
