"""Typed skill boundary; no authority fields from the model."""

from work_management_ai.agents.reporting.contracts import (
    ReportingContext as SkillInput,
)
from work_management_ai.agents.reporting.contracts import (
    ReportingNarrative as SkillOutput,
)

__all__ = ["SkillInput", "SkillOutput"]
