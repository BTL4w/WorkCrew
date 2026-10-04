"""Synthetic, provenance-linked closure fixtures; separate from production feedback."""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from work_management_ai.evaluation.phase4_daily_update import Case as DailyCase
from work_management_ai.evaluation.phase4_risk import Case as RiskCase


class EvidenceCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    locale: Literal["en", "vi"]
    scenario: str
    source_format: Literal["PDF", "DOCX", "MARKDOWN", "TEXT", "PNG", "JPEG"]
    claim: str = Field(min_length=1)
    finding: Literal["SUPPORTED", "PARTIAL", "CONTRADICTED", "UNASSESSABLE"]
    model_score: str | None
    expected_score: str | None
    expected_warnings: tuple[str, ...]
    expect_rejection: bool = False
    expected_error: str | None = None
    source_reference: Literal["CURRENT", "FOREIGN", "WRONG_VERSION"] = "CURRENT"
    inventory: Literal["COMPLETE", "OMITTED"] = "COMPLETE"
    processed_count: int = Field(default=1, ge=0, le=1)
    total_count: int = Field(default=1, ge=1, le=2)


class FixtureSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    locale: Literal["en", "vi"]
    redacted: Literal[True]
    provenance: str = Field(pattern=r"^synthetic:")
    daily_updates: tuple[DailyCase, ...] = ()
    evidence: tuple[EvidenceCase, ...] = ()
    risks: tuple[RiskCase, ...] = ()


FIXTURE_ROOT = Path(__file__).parents[3] / "tests/fixtures"


def load_fixtures(root: Path = FIXTURE_ROOT) -> tuple[FixtureSet, ...]:
    fixtures = tuple(
        FixtureSet.model_validate(json.loads((root / name).read_text()))
        for name in ("daily_update_en.json", "daily_update_vi.json", "risk_en.json", "risk_vi.json")
    )
    seen: set[str] = set()
    for fixture in fixtures:
        entries = (*fixture.daily_updates, *fixture.evidence, *fixture.risks)
        if not entries:
            raise ValueError("Empty golden fixture set")
        for entry in entries:
            id = entry.case_id if isinstance(entry, DailyCase) else entry.id
            if id in seen or entry.locale != fixture.locale:
                raise ValueError("Duplicate or mismatched golden fixture")
            seen.add(id)
            if isinstance(entry, RiskCase) and entry.scenario not in {
                "success",
                "timeout",
                "invalid",
                "unsupported",
                "revoked",
                "authority",
                "contradictory",
                "replan",
            }:
                raise ValueError("Unknown risk scenario")
    return fixtures
