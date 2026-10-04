"""Typed human-gated automation preparation contracts."""

from work_management_ai.agents.orchestrator.contracts import ScheduleIntent
from work_management_ai.runtime.contracts import DailySummaryResponseBlock

AutomationToolInput = ScheduleIntent
AutomationToolOutput = DailySummaryResponseBlock
