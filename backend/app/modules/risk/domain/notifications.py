"""Semantic state/window notification identities, independent of model prose."""

import hashlib
import json
from datetime import datetime
from typing import Literal
from uuid import UUID

from app.modules.risk.domain.assessments import Contract, RiskInputs


class RiskNotification(Contract):
    id: UUID
    task_id: UUID
    risk_id: UUID
    kind: Literal["HIGH_RISK", "SEVERE_BLOCKER", "EVIDENCE_REVIEW"]
    created_at: datetime
    read: bool = False


def notification_state(inputs: RiskInputs, band: str | None) -> str:
    # Only changes requiring a new human decision count; passage of another day,
    # daily capacity proration and different model wording must not spam recipients.
    meaningful: list[dict[str, object]] = []
    for fact in inputs.facts:
        values = dict(fact.values)
        for key in (
            "evaluation_date",
            "working_days_since_report",
            "working_days_open",
            "available_hours",
            "remaining_working_days",
            "planned_percent",
            "deviation_pp",
        ):
            values.pop(key, None)
        source = fact.id.rsplit(":", 1)[0] if fact.kind == "CAPACITY" else fact.id
        meaningful.append({"id": source, "kind": fact.kind, "values": values})
    return hashlib.sha256(
        json.dumps(
            {"facts": meaningful, "missing": inputs.missing, "band": band}, sort_keys=True
        ).encode()
    ).hexdigest()
