"""Existing evidence blocks are projected through current source access on every read."""

from typing import Any, cast
from uuid import UUID

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.retrieval.application.budget_service import RetrievalBudgetService


class RetrievalBlockProjector:
    def __init__(self, budgets: RetrievalBudgetService, previous: Any = None):
        self.budgets, self.previous = budgets, previous

    async def project(self, actor: AuthenticatedActor, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            return [await self.project(actor, item) for item in cast(list[Any], value)]
        if not isinstance(value, dict):
            return value
        value = cast(dict[str, Any], value)
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
